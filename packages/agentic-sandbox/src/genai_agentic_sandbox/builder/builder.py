"""`SandboxImageBuilder`: bake a pyproject's dependencies into a sandbox image.

The sandbox runs with no network, so the agent cannot `pip install` at run time.
Anything it needs has to be in the image already. This module builds that image
from a `pyproject.toml`:

- base: `ghcr.io/astral-sh/uv` on the latest Python (uv preinstalled)
- `[project].dependencies` installed with uv into `/opt/venv`, first on `PATH`
- a non-root `sandbox` user and a `/workspace` directory

After a build the image is verified (`verify()`): the `python` on `PATH` must
be the venv's, and every dependency must be installed *in the venv* at a version
that satisfies the pyproject. A package that only exists in the base image's
system site-packages (or at the wrong version) fails the check instead of
silently being what the agent imports.

Tags are content-addressed (`<repository>:<hash of Dockerfile + pyproject>`), so
`build()` is a no-op when nothing changed and edits to the dependency list
produce a new image instead of silently reusing a stale one.

Example:
    ```python
    from genai_agentic_sandbox.builder import SandboxImageBuilder

    builder = SandboxImageBuilder(pyproject="path/to/pyproject.toml")
    with builder.backend(mounts=[...]) as sandbox:
        print(sandbox.execute("python -c 'import pandas; print(pandas.__version__)'").output)
    ```

Requires the `docker` CLI on PATH and a reachable daemon.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.markers import InvalidMarker, Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from genai_agentic_sandbox.sandbox.docker import (
    DEFAULT_WORKDIR,
    DockerSandboxBackend,
    DockerSandboxError,
)

DEFAULT_BASE_IMAGE = "ghcr.io/astral-sh/uv:python3.14-trixie-slim"
DEFAULT_REPOSITORY = "genai-agentic-sandbox"
DEFAULT_PYPROJECT = Path(__file__).with_name("sandbox.pyproject.toml")
VENV_PATH = "/opt/venv"
SANDBOX_USER = "sandbox"
SANDBOX_UID = 1000

# Bump when the Dockerfile template changes in a way the hash would not catch
# (it is part of the tag hash).
_TEMPLATE_VERSION = "2"
_BUILD_TIMEOUT = 1800
_VERIFY_TIMEOUT = 120
_LOG_TAIL_LINES = 40

# Runs inside the image with the same `python` the sandbox resolves from PATH.
# Stdlib only: it reports facts (interpreter, marker environment, where each
# distribution is installed, `uv pip check`) and the host judges them.
_PROBE_SCRIPT = """
import importlib.metadata as md, json, os, platform, subprocess, sys

def full_version(info):
    version = f"{info.major}.{info.minor}.{info.micro}"
    if info.releaselevel != "final":
        version += info.releaselevel[0] + str(info.serial)
    return version

env = {
    "implementation_name": sys.implementation.name,
    "implementation_version": full_version(sys.implementation.version),
    "os_name": os.name,
    "platform_machine": platform.machine(),
    "platform_python_implementation": platform.python_implementation(),
    "platform_release": platform.release(),
    "platform_system": platform.system(),
    "platform_version": platform.version(),
    "python_full_version": platform.python_version(),
    "python_version": ".".join(platform.python_version_tuple()[:2]),
    "sys_platform": sys.platform,
}
dists = {}
for name in json.loads(sys.argv[1]):
    try:
        dist = md.distribution(name)
    except md.PackageNotFoundError:
        dists[name] = None
    else:
        dists[name] = {"version": dist.version, "location": str(dist.locate_file(""))}
try:
    check = subprocess.run(
        ["uv", "pip", "check", "--python", sys.executable], capture_output=True, text=True
    )
    pip_check = {"returncode": check.returncode, "output": (check.stdout + check.stderr).strip()}
except OSError as exc:
    pip_check = {"returncode": -1, "output": f"uv not runnable: {exc}"}
print(json.dumps({
    "executable": sys.executable,
    "prefix": sys.prefix,
    "env": env,
    "dists": dists,
    "pip_check": pip_check,
}))
"""


class SandboxImageBuildError(DockerSandboxError):
    """Raised when the pyproject is invalid or `docker build` fails."""


class SandboxImageVerificationError(SandboxImageBuildError):
    """Raised when a built image does not provide the pyproject's packages."""

    def __init__(self, report: VerificationReport) -> None:
        self.report = report
        super().__init__(f"sandbox image {report.image} failed verification:\n{report.summary()}")


@dataclass(frozen=True)
class PackageCheck:
    """How one pyproject requirement is satisfied inside the image."""

    requirement: str
    name: str
    installed_version: str | None = None
    location: str | None = None
    skipped: bool = False
    problem: str | None = None

    @property
    def ok(self) -> bool:
        return self.problem is None


@dataclass(frozen=True)
class VerificationReport:
    """Result of `SandboxImageBuilder.verify()`."""

    image: str
    executable: str
    prefix: str
    packages: list[PackageCheck]
    problems: list[str]
    pip_check_output: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems and all(p.ok for p in self.packages)

    @property
    def installed(self) -> dict[str, str]:
        """`{name: version}` for every requirement found in the venv."""
        return {p.name: p.installed_version for p in self.packages if p.installed_version and p.ok}

    def summary(self) -> str:
        lines = [f"python: {self.executable} (prefix {self.prefix})"]
        lines += [f"  ! {problem}" for problem in self.problems]
        for pkg in self.packages:
            if pkg.skipped:
                lines.append(f"  - {pkg.requirement}: skipped (marker does not apply)")
            elif pkg.ok:
                lines.append(f"  ok {pkg.requirement}: {pkg.installed_version}")
            else:
                lines.append(f"  x {pkg.requirement}: {pkg.problem}")
        return "\n".join(lines)


@dataclass
class SandboxImageBuilder:
    """Builds (and caches) a sandbox image from a `pyproject.toml`.

    Args:
        pyproject: File whose `[project].dependencies` are installed into the
            image. Defaults to `sandbox.pyproject.toml` next to this module.
        base_image: Image to build from. Must ship `uv`, `python3`, a POSIX
            shell and coreutils `timeout`.
        repository: Image name; the tag is derived from the content hash.
        apt_packages: Extra Debian packages (e.g. `["git", "graphviz"]`).
        workdir: Working directory created in the image; keep it equal to the
            backend's `workdir`.
        docker_bin: The CLI to drive.
        platform: Optional `--platform` for the build (e.g. `"linux/amd64"`).
        build_timeout: Seconds before `docker build` is abandoned.
    """

    pyproject: str | Path = DEFAULT_PYPROJECT
    base_image: str = DEFAULT_BASE_IMAGE
    repository: str = DEFAULT_REPOSITORY
    apt_packages: list[str] = field(default_factory=list)
    workdir: str = DEFAULT_WORKDIR
    docker_bin: str = "docker"
    platform: str | None = None
    build_timeout: int = _BUILD_TIMEOUT

    def __post_init__(self) -> None:
        self.pyproject = Path(self.pyproject).expanduser().resolve()
        self._pyproject_bytes = self._load_pyproject(self.pyproject)

    # -- description -------------------------------------------------------

    @property
    def dependencies(self) -> list[str]:
        """The `[project].dependencies` that will be installed."""
        return list(tomllib.loads(self._pyproject_bytes.decode())["project"]["dependencies"])

    @property
    def tag(self) -> str:
        """`<repository>:<12-char content hash>`; changes whenever the image would."""
        digest = hashlib.sha256()
        for part in (_TEMPLATE_VERSION.encode(), self.dockerfile().encode(), self._pyproject_bytes):
            digest.update(part)
            digest.update(b"\0")
        if self.platform:
            digest.update(self.platform.encode())
        return f"{self.repository}:{digest.hexdigest()[:12]}"

    def dockerfile(self) -> str:
        """Render the Dockerfile used for the build."""
        lines = [
            f"FROM {self.base_image}",
            "",
            "ENV UV_COMPILE_BYTECODE=1 \\",
            "    UV_LINK_MODE=copy \\",
            "    UV_NO_CACHE=1 \\",
            "    UV_PYTHON_DOWNLOADS=never \\",
            f"    VIRTUAL_ENV={VENV_PATH} \\",
            f"    PATH={VENV_PATH}/bin:$PATH \\",
            "    PYTHONDONTWRITEBYTECODE=1 \\",
            # User site-packages (~/.local, under the writable workdir) would
            # otherwise shadow what was installed into the venv.
            "    PYTHONNOUSERSITE=1 \\",
            "    PYTHONUNBUFFERED=1",
            "",
        ]
        if self.apt_packages:
            packages = " ".join(sorted(set(self.apt_packages)))
            lines += [
                "RUN apt-get update \\",
                f" && apt-get install -y --no-install-recommends {packages} \\",
                " && rm -rf /var/lib/apt/lists/*",
                "",
            ]
        lines += [
            # Only the pyproject is copied, so the dependency layer is cached
            # until the dependency list actually changes.
            "COPY pyproject.toml /opt/sandbox/pyproject.toml",
            f"RUN uv venv {VENV_PATH} --python /usr/local/bin/python3 \\",
            f" && uv pip install --python {VENV_PATH}/bin/python -r /opt/sandbox/pyproject.toml",
            "",
            f"RUN useradd --uid {SANDBOX_UID} --user-group --create-home --shell /bin/sh {SANDBOX_USER} \\",
            f" && mkdir -p {self.workdir} \\",
            f" && chown {SANDBOX_USER}:{SANDBOX_USER} {self.workdir}",
            "",
            f"USER {SANDBOX_USER}",
            f"WORKDIR {self.workdir}",
            'CMD ["/bin/sh"]',
            "",
        ]
        return "\n".join(lines)

    # -- docker ------------------------------------------------------------

    def exists(self) -> bool:
        """Whether an image with the current `tag` is present locally."""
        probe = self._docker(["image", "inspect", self.tag], timeout=60)
        return probe.returncode == 0

    def build(self, *, force: bool = False, pull: bool = False, verify: bool = True) -> str:
        """Build the image unless an identical one already exists.

        Args:
            force: Rebuild even when the tag is present (`--no-cache`).
            pull: Always pull a newer `base_image` before building.
            verify: Run `verify()` on the image (built or cached) and raise if
                any dependency is missing, mismatched, or not from the venv.

        Returns:
            The image tag, ready to pass as `DockerSandboxBackend(image=...)`.

        Raises:
            SandboxImageBuildError: If Docker is unavailable or the build fails.
            SandboxImageVerificationError: If `verify` is set and the check fails.
        """
        tag = self.tag
        if force or not self.exists():
            self._build(tag, force=force, pull=pull)
        if verify:
            report = self.verify(tag)
            if not report.ok:
                raise SandboxImageVerificationError(report)
        return tag

    def verify(self, image: str | None = None) -> VerificationReport:
        """Check that `image` really provides this pyproject's dependencies.

        Runs a probe in a throw-away container (no network) using the `python`
        the sandbox itself resolves, then checks that:

        - that interpreter is the venv's (`/opt/venv`), not the base image's;
        - every requirement whose marker applies is installed *inside the venv*
          (a copy in the base image's system site-packages does not count);
        - the installed version satisfies the requirement's specifier;
        - `uv pip check` finds no broken or conflicting dependencies.

        Args:
            image: Image to check; defaults to this builder's `tag`.

        Raises:
            SandboxImageBuildError: If the probe cannot run at all.
        """
        image = image or self.tag
        requirements = [Requirement(dep) for dep in self.dependencies]
        names = sorted({canonicalize_name(req.name) for req in requirements})
        args = ["run", "--rm", "--network", "none"]
        if self.platform:
            args += ["--platform", self.platform]
        args += [image, "python", "-c", _PROBE_SCRIPT, json.dumps(names)]
        try:
            result = self._docker(args, timeout=_VERIFY_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            msg = f"verifying {image} timed out after {_VERIFY_TIMEOUT}s"
            raise SandboxImageBuildError(msg) from exc
        try:
            facts = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            detail = (result.stderr or result.stdout or "").strip()
            msg = f"could not probe {image} (exit {result.returncode}): {detail}"
            raise SandboxImageBuildError(msg) from exc
        return _judge(image, requirements, facts)

    def _build(self, tag: str, *, force: bool, pull: bool) -> None:
        args = ["build", "--tag", tag, "--tag", f"{self.repository}:latest"]
        args += ["--label", f"genai.sandbox.pyproject={self.pyproject.name}"]
        if self.platform:
            args += ["--platform", self.platform]
        if force:
            args.append("--no-cache")
        if pull:
            args.append("--pull")

        with tempfile.TemporaryDirectory(prefix="sandbox-build-") as ctx:
            context = Path(ctx)
            (context / "Dockerfile").write_text(self.dockerfile())
            (context / "pyproject.toml").write_bytes(self._pyproject_bytes)
            try:
                result = self._docker([*args, str(context)], timeout=self.build_timeout)
            except subprocess.TimeoutExpired as exc:
                msg = f"docker build timed out after {self.build_timeout}s"
                raise SandboxImageBuildError(msg) from exc

        if result.returncode != 0:
            log = (result.stderr or "") + (result.stdout or "")
            tail = "\n".join(log.strip().splitlines()[-_LOG_TAIL_LINES:])
            msg = f"docker build failed for {tag}:\n{tail}"
            raise SandboxImageBuildError(msg)

    def backend(
        self, *, force: bool = False, pull: bool = False, verify: bool = True, **kwargs: Any
    ) -> DockerSandboxBackend:
        """Build (if needed), verify, and return a `DockerSandboxBackend` on this image.

        Keyword arguments are passed to `DockerSandboxBackend`; `image` and
        `workdir` are set from this builder.
        """
        tag = self.build(force=force, pull=pull, verify=verify)
        return DockerSandboxBackend(
            image=tag, workdir=self.workdir, docker_bin=self.docker_bin, **kwargs
        )

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _load_pyproject(path: Path) -> bytes:
        if not path.is_file():
            msg = f"pyproject not found: {path}"
            raise SandboxImageBuildError(msg)
        raw = path.read_bytes()
        try:
            data = tomllib.loads(raw.decode())
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            msg = f"{path} is not valid TOML: {exc}"
            raise SandboxImageBuildError(msg) from exc
        deps = data.get("project", {}).get("dependencies")
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            msg = f"{path} needs a [project] table with a `dependencies` list of strings"
            raise SandboxImageBuildError(msg)
        for dep in deps:
            try:
                Requirement(dep)
            except (InvalidRequirement, InvalidMarker) as exc:
                msg = f"{path}: invalid dependency {dep!r}: {exc}"
                raise SandboxImageBuildError(msg) from exc
        return raw

    def _docker(self, args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [self.docker_bin, *args],
                check=False,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
                start_new_session=(sys.platform != "win32"),
            )
        except FileNotFoundError as exc:
            msg = f"{self.docker_bin!r} is not on PATH. Install Docker to build sandbox images."
            raise SandboxImageBuildError(msg) from exc


def _judge(
    image: str, requirements: list[Requirement], facts: dict[str, Any]
) -> VerificationReport:
    """Turn the in-image probe output into a `VerificationReport`."""
    venv = VENV_PATH.rstrip("/") + "/"
    executable, prefix = facts["executable"], facts["prefix"]
    problems: list[str] = []
    if not executable.startswith(venv):
        problems.append(
            f"`python` on PATH is {executable}, not the venv's ({VENV_PATH}/bin/python)"
        )
    if prefix.rstrip("/") != VENV_PATH:
        problems.append(f"interpreter prefix is {prefix}, expected {VENV_PATH}")
    pip_check = facts.get("pip_check", {})
    if pip_check.get("returncode") != 0:
        problems.append(f"uv pip check failed: {pip_check.get('output', '')}")

    env = facts["env"]
    packages: list[PackageCheck] = []
    for req in requirements:
        name = canonicalize_name(req.name)
        if req.marker is not None and not Marker(str(req.marker)).evaluate(env):
            packages.append(PackageCheck(str(req), name, skipped=True))
            continue
        dist = facts["dists"].get(name)
        if dist is None:
            packages.append(PackageCheck(str(req), name, problem="not installed"))
            continue
        version, location = dist["version"], dist["location"]
        problem = None
        if not location.startswith(venv):
            problem = f"resolved from {location}, outside the venv (base image copy?)"
        elif req.specifier and not req.specifier.contains(version, prereleases=True):
            problem = f"installed {version} does not satisfy {req.specifier}"
        packages.append(PackageCheck(str(req), name, version, location, problem=problem))

    return VerificationReport(
        image=image,
        executable=executable,
        prefix=prefix,
        packages=packages,
        problems=problems,
        pip_check_output=pip_check.get("output", ""),
    )


def main(argv: list[str] | None = None) -> int:
    """`python -m genai_agentic_sandbox.builder [--pyproject FILE] [--force] [--pull]`."""
    import argparse

    parser = argparse.ArgumentParser(description="Build the agentic sandbox Docker image.")
    parser.add_argument("--pyproject", type=Path, default=DEFAULT_PYPROJECT)
    parser.add_argument("--base-image", default=DEFAULT_BASE_IMAGE)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--apt", nargs="*", default=[], help="extra Debian packages")
    parser.add_argument("--force", action="store_true", help="rebuild without cache")
    parser.add_argument("--pull", action="store_true", help="pull a newer base image")
    parser.add_argument("--print-dockerfile", action="store_true")
    parser.add_argument("--no-verify", action="store_true", help="skip the package check")
    parser.add_argument(
        "--verify-only", action="store_true", help="check the existing image, do not build"
    )
    ns = parser.parse_args(argv)

    try:
        builder = SandboxImageBuilder(
            pyproject=ns.pyproject,
            base_image=ns.base_image,
            repository=ns.repository,
            apt_packages=ns.apt,
        )
        if ns.print_dockerfile:
            print(builder.dockerfile(), end="")
            return 0
        if ns.verify_only:
            report = builder.verify()
            print(report.summary())
            return 0 if report.ok else 1
        tag = builder.build(force=ns.force, pull=ns.pull, verify=not ns.no_verify)
        print(tag)
    except SandboxImageVerificationError as exc:
        print(exc.report.summary(), file=sys.stderr)
        print(f"error: {exc.report.image} failed verification", file=sys.stderr)
        return 1
    except SandboxImageBuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


__all__ = [
    "DEFAULT_BASE_IMAGE",
    "DEFAULT_PYPROJECT",
    "DEFAULT_REPOSITORY",
    "PackageCheck",
    "SandboxImageBuildError",
    "SandboxImageBuilder",
    "SandboxImageVerificationError",
    "VerificationReport",
    "main",
]
