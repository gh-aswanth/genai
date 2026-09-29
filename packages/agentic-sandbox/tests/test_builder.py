"""Unit tests for `SandboxImageBuilder`. No Docker daemon needed."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from genai_agentic_sandbox.builder import (
    DEFAULT_BASE_IMAGE,
    DEFAULT_PYPROJECT,
    SandboxImageBuilder,
    SandboxImageBuildError,
    SandboxImageVerificationError,
    main,
)
from genai_agentic_sandbox.sandbox.docker import DockerSandboxBackend, DockerSandboxError


def cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


PYPROJECT = """\
[project]
name = "demo"
version = "0.1.0"
dependencies = ["tabulate>=0.9", "pyyaml"]
"""


@pytest.fixture
def pyproject(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text(PYPROJECT)
    return path


VENV_SITE = "/opt/venv/lib/python3.14/site-packages"
SYSTEM_SITE = "/usr/local/lib/python3.14/site-packages"
LINUX_ENV = {
    "implementation_name": "cpython",
    "implementation_version": "3.14.7",
    "os_name": "posix",
    "platform_machine": "aarch64",
    "platform_python_implementation": "CPython",
    "platform_release": "6.10",
    "platform_system": "Linux",
    "platform_version": "#1 SMP",
    "python_full_version": "3.14.7",
    "python_version": "3.14",
    "sys_platform": "linux",
}


def probe_facts(dists=None, *, executable="/opt/venv/bin/python", prefix="/opt/venv", pip_check=0):
    """What the in-image probe prints; defaults to a healthy image for PYPROJECT."""
    if dists is None:
        dists = {
            "tabulate": {"version": "0.9.0", "location": VENV_SITE},
            "pyyaml": {"version": "6.0.3", "location": VENV_SITE},
        }
    return {
        "executable": executable,
        "prefix": prefix,
        "env": LINUX_ENV,
        "dists": dists,
        "pip_check": {"returncode": pip_check, "output": "" if pip_check == 0 else "x has y"},
    }


class FakeDocker:
    """Stands in for `_docker`; records calls, captures the build context, answers the probe."""

    def __init__(
        self,
        *,
        image_present: bool = False,
        build: subprocess.CompletedProcess | None = None,
        probe: dict | subprocess.CompletedProcess | None = None,
    ):
        self.image_present = image_present
        self.build_result = build or cp(0)
        self.probe = probe_facts() if probe is None else probe
        self.calls: list[list[str]] = []
        self.context_files: dict[str, str] = {}

    def __call__(self, args, *, timeout):
        self.calls.append(args)
        if args[:2] == ["image", "inspect"]:
            return cp(0 if self.image_present else 1)
        if args[0] == "build":
            context = Path(args[-1])
            self.context_files = {p.name: p.read_text() for p in context.iterdir()}
            return self.build_result
        if args[0] == "run":
            if isinstance(self.probe, subprocess.CompletedProcess):
                return self.probe
            return cp(0, json.dumps(self.probe) + "\n")
        raise AssertionError(f"unexpected docker call: {args}")

    def commands(self) -> list[str]:
        return [c[0] for c in self.calls]

    @property
    def run_args(self) -> list[str]:
        return next(c for c in self.calls if c[0] == "run")

    @property
    def build_args(self) -> list[str]:
        return next(c for c in self.calls if c[0] == "build")


# -- pyproject validation ----------------------------------------------------------------


class TestPyproject:
    def test_default_pyproject_is_valid(self):
        builder = SandboxImageBuilder()
        assert builder.pyproject == DEFAULT_PYPROJECT
        assert builder.dependencies  # non-empty

    def test_dependencies_are_read(self, pyproject):
        assert SandboxImageBuilder(pyproject=pyproject).dependencies == ["tabulate>=0.9", "pyyaml"]

    def test_missing_file(self, tmp_path):
        with pytest.raises(SandboxImageBuildError, match="not found"):
            SandboxImageBuilder(pyproject=tmp_path / "nope.toml")

    def test_invalid_toml(self, tmp_path):
        bad = tmp_path / "pyproject.toml"
        bad.write_text("[project\nname=")
        with pytest.raises(SandboxImageBuildError, match="not valid TOML"):
            SandboxImageBuilder(pyproject=bad)

    @pytest.mark.parametrize(
        "content",
        [
            "[tool.x]\na = 1\n",
            '[project]\nname = "x"\n',
            '[project]\nname = "x"\ndependencies = "numpy"\n',
            '[project]\nname = "x"\ndependencies = [1, 2]\n',
        ],
    )
    def test_needs_project_dependencies_list(self, tmp_path, content):
        path = tmp_path / "pyproject.toml"
        path.write_text(content)
        with pytest.raises(SandboxImageBuildError, match="dependencies"):
            SandboxImageBuilder(pyproject=path)

    def test_empty_dependency_list_is_allowed(self, tmp_path):
        path = tmp_path / "pyproject.toml"
        path.write_text('[project]\nname = "x"\nversion = "0"\ndependencies = []\n')
        assert SandboxImageBuilder(pyproject=path).dependencies == []

    def test_invalid_requirement_is_rejected(self, tmp_path):
        path = tmp_path / "pyproject.toml"
        path.write_text('[project]\nname = "x"\nversion = "0"\ndependencies = ["pandas >>= 2"]\n')
        with pytest.raises(SandboxImageBuildError, match="invalid dependency"):
            SandboxImageBuilder(pyproject=path)

    def test_build_error_is_a_sandbox_error(self):
        assert issubclass(SandboxImageBuildError, DockerSandboxError)


# -- Dockerfile ------------------------------------------------------------------------


class TestDockerfile:
    def test_uses_uv_python_base_image(self, pyproject):
        dockerfile = SandboxImageBuilder(pyproject=pyproject).dockerfile()
        assert dockerfile.startswith(f"FROM {DEFAULT_BASE_IMAGE}\n")
        assert DEFAULT_BASE_IMAGE.startswith("ghcr.io/astral-sh/uv:python")

    def test_installs_pyproject_into_venv_with_uv(self, pyproject):
        dockerfile = SandboxImageBuilder(pyproject=pyproject).dockerfile()
        assert "COPY pyproject.toml /opt/sandbox/pyproject.toml" in dockerfile
        assert "uv venv /opt/venv" in dockerfile
        assert (
            "uv pip install --python /opt/venv/bin/python -r /opt/sandbox/pyproject.toml"
            in dockerfile
        )
        assert "PATH=/opt/venv/bin:$PATH" in dockerfile
        assert "UV_PYTHON_DOWNLOADS=never" in dockerfile

    def test_venv_is_isolated_from_system_and_user_site_packages(self, pyproject):
        dockerfile = SandboxImageBuilder(pyproject=pyproject).dockerfile()
        assert "--system-site-packages" not in dockerfile
        assert "PYTHONNOUSERSITE=1" in dockerfile

    def test_runs_as_non_root_in_workdir(self, pyproject):
        dockerfile = SandboxImageBuilder(pyproject=pyproject, workdir="/work").dockerfile()
        assert "useradd --uid 1000" in dockerfile
        assert "USER sandbox" in dockerfile
        assert "WORKDIR /work" in dockerfile
        assert dockerfile.index("uv pip install") < dockerfile.index("USER sandbox")

    def test_no_apt_layer_by_default(self, pyproject):
        assert "apt-get" not in SandboxImageBuilder(pyproject=pyproject).dockerfile()

    def test_apt_packages_are_sorted_and_deduplicated(self, pyproject):
        dockerfile = SandboxImageBuilder(
            pyproject=pyproject, apt_packages=["git", "curl", "git"]
        ).dockerfile()
        assert "apt-get install -y --no-install-recommends curl git" in dockerfile
        assert "rm -rf /var/lib/apt/lists/*" in dockerfile

    def test_custom_base_image(self, pyproject):
        dockerfile = SandboxImageBuilder(pyproject=pyproject, base_image="my/uv:py").dockerfile()
        assert dockerfile.startswith("FROM my/uv:py\n")


# -- tag -------------------------------------------------------------------------------


class TestTag:
    def test_tag_is_stable(self, pyproject):
        assert (
            SandboxImageBuilder(pyproject=pyproject).tag
            == SandboxImageBuilder(pyproject=pyproject).tag
        )

    def test_tag_format(self, pyproject):
        repo, digest = SandboxImageBuilder(pyproject=pyproject, repository="my-sbx").tag.split(":")
        assert repo == "my-sbx"
        assert len(digest) == 12
        int(digest, 16)

    def test_tag_changes_with_dependencies(self, pyproject):
        before = SandboxImageBuilder(pyproject=pyproject).tag
        pyproject.write_text(PYPROJECT.replace('"pyyaml"', '"pyyaml>=6"'))
        assert SandboxImageBuilder(pyproject=pyproject).tag != before

    @pytest.mark.parametrize(
        "change",
        [
            {"base_image": "other:1"},
            {"apt_packages": ["git"]},
            {"workdir": "/w"},
            {"platform": "linux/amd64"},
        ],
    )
    def test_tag_changes_with_build_inputs(self, pyproject, change):
        assert (
            SandboxImageBuilder(pyproject=pyproject, **change).tag
            != SandboxImageBuilder(pyproject=pyproject).tag
        )


# -- build -----------------------------------------------------------------------------


class TestBuild:
    def make(self, monkeypatch, pyproject, docker: FakeDocker, **kwargs) -> SandboxImageBuilder:
        builder = SandboxImageBuilder(pyproject=pyproject, **kwargs)
        monkeypatch.setattr(builder, "_docker", docker)
        return builder

    def test_skips_build_when_image_exists(self, monkeypatch, pyproject):
        docker = FakeDocker(image_present=True)
        builder = self.make(monkeypatch, pyproject, docker)
        assert builder.build(verify=False) == builder.tag
        assert docker.commands() == ["image"]

    def test_cached_image_is_still_verified(self, monkeypatch, pyproject):
        docker = FakeDocker(image_present=True)
        self.make(monkeypatch, pyproject, docker).build()
        assert docker.commands() == ["image", "run"]

    def test_builds_when_missing(self, monkeypatch, pyproject):
        docker = FakeDocker()
        builder = self.make(monkeypatch, pyproject, docker)
        assert builder.build() == builder.tag
        args = docker.build_args
        assert args[args.index("--tag") + 1] == builder.tag
        assert f"{builder.repository}:latest" in args
        assert "--no-cache" not in args and "--pull" not in args

    def test_context_contains_only_dockerfile_and_pyproject(self, monkeypatch, pyproject):
        docker = FakeDocker()
        builder = self.make(monkeypatch, pyproject, docker)
        builder.build()
        assert docker.context_files == {
            "Dockerfile": builder.dockerfile(),
            "pyproject.toml": PYPROJECT,
        }

    def test_non_pyproject_filename_is_copied_as_pyproject(self, monkeypatch, tmp_path):
        custom = tmp_path / "sandbox.deps.toml"
        custom.write_text(PYPROJECT)
        docker = FakeDocker()
        self.make(monkeypatch, custom, docker).build()
        assert docker.context_files["pyproject.toml"] == PYPROJECT

    def test_force_rebuilds_without_cache(self, monkeypatch, pyproject):
        docker = FakeDocker(image_present=True)
        self.make(monkeypatch, pyproject, docker).build(force=True, pull=True)
        assert "--no-cache" in docker.build_args
        assert "--pull" in docker.build_args

    def test_platform_is_passed(self, monkeypatch, pyproject):
        docker = FakeDocker()
        self.make(monkeypatch, pyproject, docker, platform="linux/amd64").build()
        args = docker.build_args
        assert args[args.index("--platform") + 1] == "linux/amd64"

    def test_build_failure_shows_log_tail(self, monkeypatch, pyproject):
        log = "\n".join(f"line {i}" for i in range(100)) + "\nerror: No solution found"
        docker = FakeDocker(build=cp(1, "", log))
        builder = self.make(monkeypatch, pyproject, docker)
        with pytest.raises(SandboxImageBuildError, match="No solution found") as info:
            builder.build()
        assert "line 0\n" not in str(info.value)

    def test_build_timeout(self, monkeypatch, pyproject):
        builder = SandboxImageBuilder(pyproject=pyproject, build_timeout=1)

        def fake(args, *, timeout):
            if args[0] == "build":
                raise subprocess.TimeoutExpired(cmd="docker", timeout=timeout)
            return cp(1)

        monkeypatch.setattr(builder, "_docker", fake)
        with pytest.raises(SandboxImageBuildError, match="timed out"):
            builder.build()

    def test_missing_docker_cli(self, pyproject):
        builder = SandboxImageBuilder(
            pyproject=pyproject, docker_bin="definitely-not-a-docker-binary"
        )
        with pytest.raises(SandboxImageBuildError, match="not on PATH"):
            builder.build()

    def test_backend_uses_built_image(self, monkeypatch, pyproject):
        builder = self.make(monkeypatch, pyproject, FakeDocker(image_present=True), workdir="/work")
        backend = builder.backend(network="bridge", timeout=5)
        assert isinstance(backend, DockerSandboxBackend)
        assert backend.image == builder.tag
        assert backend.workdir == "/work"
        assert backend.network == "bridge"
        assert backend.timeout == 5


# -- CLI -------------------------------------------------------------------------------


class TestCli:
    def test_print_dockerfile(self, pyproject, capsys):
        assert main(["--pyproject", str(pyproject), "--print-dockerfile"]) == 0
        assert capsys.readouterr().out == SandboxImageBuilder(pyproject=pyproject).dockerfile()

    def test_bad_pyproject_exits_1(self, tmp_path, capsys):
        assert main(["--pyproject", str(tmp_path / "nope.toml")]) == 1
        assert "error:" in capsys.readouterr().err

    def test_build_prints_tag(self, monkeypatch, pyproject, capsys):
        monkeypatch.setattr(SandboxImageBuilder, "build", lambda self, **_: "repo:abc")
        assert main(["--pyproject", str(pyproject)]) == 0
        assert capsys.readouterr().out.strip() == "repo:abc"


# -- verification ------------------------------------------------------------------------


class TestVerify:
    def make(self, monkeypatch, pyproject, docker: FakeDocker) -> SandboxImageBuilder:
        builder = SandboxImageBuilder(pyproject=pyproject)
        monkeypatch.setattr(builder, "_docker", docker)
        return builder

    def test_healthy_image(self, monkeypatch, pyproject):
        report = self.make(monkeypatch, pyproject, FakeDocker()).verify()
        assert report.ok
        assert report.installed == {"tabulate": "0.9.0", "pyyaml": "6.0.3"}
        assert "ok tabulate>=0.9: 0.9.0" in report.summary()

    def test_probe_runs_offline_with_the_sandbox_python(self, monkeypatch, pyproject):
        docker = FakeDocker()
        builder = self.make(monkeypatch, pyproject, docker)
        builder.verify()
        args = docker.run_args
        assert args[:4] == ["run", "--rm", "--network", "none"]
        image_at = args.index(builder.tag)
        assert args[image_at + 1 : image_at + 3] == ["python", "-c"]
        assert json.loads(args[-1]) == ["pyyaml", "tabulate"]  # canonical names

    def test_package_only_in_base_image_system_site_packages(self, monkeypatch, pyproject):
        # The case this check exists for: the base image ships a copy, the venv does not.
        facts = probe_facts(
            {
                "tabulate": {"version": "0.9.0", "location": SYSTEM_SITE},
                "pyyaml": {"version": "6.0.3", "location": VENV_SITE},
            }
        )
        report = self.make(monkeypatch, pyproject, FakeDocker(probe=facts)).verify()
        assert not report.ok
        [bad] = [p for p in report.packages if not p.ok]
        assert bad.name == "tabulate"
        assert "outside the venv" in bad.problem
        assert "tabulate" not in report.installed

    def test_missing_package(self, monkeypatch, pyproject):
        facts = probe_facts(
            {"tabulate": None, "pyyaml": {"version": "6.0.3", "location": VENV_SITE}}
        )
        report = self.make(monkeypatch, pyproject, FakeDocker(probe=facts)).verify()
        assert [(p.name, p.problem) for p in report.packages if not p.ok] == [
            ("tabulate", "not installed")
        ]

    def test_version_does_not_satisfy_specifier(self, monkeypatch, pyproject):
        facts = probe_facts(
            {
                "tabulate": {"version": "0.8.10", "location": VENV_SITE},
                "pyyaml": {"version": "6.0.3", "location": VENV_SITE},
            }
        )
        report = self.make(monkeypatch, pyproject, FakeDocker(probe=facts)).verify()
        [bad] = [p for p in report.packages if not p.ok]
        assert "0.8.10 does not satisfy >=0.9" in bad.problem

    def test_python_on_path_is_not_the_venv(self, monkeypatch, pyproject):
        facts = probe_facts(executable="/usr/local/bin/python3", prefix="/usr/local")
        report = self.make(monkeypatch, pyproject, FakeDocker(probe=facts)).verify()
        assert not report.ok
        assert any("not the venv's" in p for p in report.problems)
        assert any("prefix is /usr/local" in p for p in report.problems)

    def test_pip_check_failure(self, monkeypatch, pyproject):
        report = self.make(
            monkeypatch, pyproject, FakeDocker(probe=probe_facts(pip_check=1))
        ).verify()
        assert not report.ok
        assert any("uv pip check failed" in p for p in report.problems)

    def test_marker_that_does_not_apply_is_skipped(self, monkeypatch, tmp_path):
        path = tmp_path / "pyproject.toml"
        path.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            'dependencies = ["pywin32; sys_platform == \'win32\'", "tabulate"]\n'
        )
        facts = probe_facts(
            {"pywin32": None, "tabulate": {"version": "0.9.0", "location": VENV_SITE}}
        )
        report = self.make(monkeypatch, path, FakeDocker(probe=facts)).verify()
        assert report.ok
        [skipped] = [p for p in report.packages if p.skipped]
        assert skipped.name == "pywin32"

    def test_extras_check_the_base_distribution(self, monkeypatch, tmp_path):
        path = tmp_path / "pyproject.toml"
        path.write_text(
            '[project]\nname = "x"\nversion = "0"\ndependencies = ["Tabulate[widechars]>=0.9"]\n'
        )
        facts = probe_facts({"tabulate": {"version": "0.9.0", "location": VENV_SITE}})
        docker = FakeDocker(probe=facts)
        assert self.make(monkeypatch, path, docker).verify().ok
        assert json.loads(docker.run_args[-1]) == ["tabulate"]

    def test_unparseable_probe_output(self, monkeypatch, pyproject):
        docker = FakeDocker(probe=cp(125, "", "Unable to find image"))
        with pytest.raises(SandboxImageBuildError, match="could not probe.*Unable to find image"):
            self.make(monkeypatch, pyproject, docker).verify()

    def test_build_raises_when_verification_fails(self, monkeypatch, pyproject):
        facts = probe_facts({"tabulate": None, "pyyaml": None})
        builder = self.make(monkeypatch, pyproject, FakeDocker(probe=facts))
        with pytest.raises(SandboxImageVerificationError) as info:
            builder.build()
        assert not info.value.report.ok
        assert "tabulate>=0.9: not installed" in str(info.value)

    def test_build_without_verify_skips_probe(self, monkeypatch, pyproject):
        docker = FakeDocker(probe=probe_facts({"tabulate": None, "pyyaml": None}))
        self.make(monkeypatch, pyproject, docker).build(verify=False)
        assert "run" not in docker.commands()

    def test_backend_refuses_unverified_image(self, monkeypatch, pyproject):
        builder = self.make(
            monkeypatch, pyproject, FakeDocker(image_present=True, probe=probe_facts(pip_check=1))
        )
        with pytest.raises(SandboxImageVerificationError):
            builder.backend()
        assert builder.backend(verify=False).image == builder.tag

    def test_verify_other_image(self, monkeypatch, pyproject):
        docker = FakeDocker()
        report = self.make(monkeypatch, pyproject, docker).verify("some/image:1")
        assert report.image == "some/image:1"
        assert "some/image:1" in docker.run_args


class TestCliVerify:
    def test_verify_only_ok(self, monkeypatch, pyproject, capsys):
        monkeypatch.setattr(
            SandboxImageBuilder, "_docker", lambda self, args, **kw: FakeDocker()(args, **kw)
        )
        assert main(["--pyproject", str(pyproject), "--verify-only"]) == 0
        assert "ok tabulate" in capsys.readouterr().out

    def test_verify_only_failure_exits_1(self, monkeypatch, pyproject, capsys):
        docker = FakeDocker(probe=probe_facts({"tabulate": None, "pyyaml": None}))
        monkeypatch.setattr(
            SandboxImageBuilder, "_docker", lambda self, args, **kw: docker(args, **kw)
        )
        assert main(["--pyproject", str(pyproject), "--verify-only"]) == 1
        assert "not installed" in capsys.readouterr().out

    def test_build_failing_verification_exits_1(self, monkeypatch, pyproject, capsys):
        docker = FakeDocker(
            image_present=True, probe=probe_facts({"tabulate": None, "pyyaml": None})
        )
        monkeypatch.setattr(
            SandboxImageBuilder, "_docker", lambda self, args, **kw: docker(args, **kw)
        )
        assert main(["--pyproject", str(pyproject)]) == 1
        assert "failed verification" in capsys.readouterr().err

    def test_no_verify_flag(self, monkeypatch, pyproject, capsys):
        seen = {}
        monkeypatch.setattr(
            SandboxImageBuilder, "build", lambda self, **kw: seen.update(kw) or "repo:abc"
        )
        assert main(["--pyproject", str(pyproject), "--no-verify"]) == 0
        assert seen["verify"] is False
