"""`DockerSandboxBackend`: a hardened, mount-aware Docker backend for deepagents.

`LocalShellBackend` runs the agent's shell straight on the host: no isolation,
no resource limits, and `virtual_mode` buys nothing because `execute()` can
reach any path. This module trades that for a container.

Every file operation and every shell command is executed inside one long-lived
container via `docker exec`. The only host paths the agent can touch are the
ones declared up-front in `mounts=[...]`, which is what makes "mount the device
folder, write results back to it" safe to do: the device folder is bind-mounted
read-only, an output folder is bind-mounted read-write, and nothing else on the
host is visible.

Defaults are deliberately restrictive (no network, read-only root filesystem,
all capabilities dropped, memory/CPU/PID caps). Loosen them per-argument when a
run genuinely needs it.

Example:
    ```python
    from genai_agentic_sandbox.sandbox.docker import DockerSandboxBackend, Mount

    backend = DockerSandboxBackend(
        image="python:3.13-slim",
        mounts=[
            # Device data goes in read-only: the agent cannot corrupt it.
            Mount("/Volumes/DEVICE/captures", "/mnt/device", read_only=True),
            # Results come back out through a writable mount.
            Mount("./output", "/mnt/output", read_only=False, create=True),
        ],
    )
    print(backend.execute("ls /mnt/device").output)
    backend.write("/mnt/output/report.txt", "done")   # lands on the host
    backend.close()
    ```

Requires the `docker` CLI on PATH and a reachable daemon; `podman` works too
via `docker_bin="podman"`.
"""

from __future__ import annotations

import os
import platform
import shlex
import subprocess
import sys
import uuid
import weakref
from dataclasses import dataclass, field
from pathlib import Path

from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    IS_DIRECTORY,
    PERMISSION_DENIED,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

DEFAULT_IMAGE = "python:3.13-slim"
DEFAULT_WORKDIR = "/workspace"
DEFAULT_EXECUTE_TIMEOUT = 120
DEFAULT_MAX_OUTPUT_BYTES = 100_000
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024

# Docker CLI calls that are not the agent's command (run/inspect/rm/cp). Bounded
# separately so a wedged daemon surfaces as an error instead of a hang.
_DOCKER_CTL_TIMEOUT = 120
_DOCKER_PULL_TIMEOUT = 900

# Host paths that must never be bind-mounted into an agent-controlled container.
# Mounting any of these hands the agent the host: `/` is self-explanatory, the
# docker socket is a one-command container escape (it can start a new privileged
# container mounting `/`), and the credential directories are what an escape is
# usually after.
_DENIED_HOST_PATHS = frozenset(
    {
        "/",
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/lib",
        "/proc",
        "/private/etc",
        "/private/var",
        "/root",
        "/sbin",
        "/sys",
        "/usr",
        "/var",
        "/Library",
        "/System",
        "/Applications",
        "/Users",
        "/home",
    }
)
_DENIED_HOST_NAMES = frozenset(
    {".ssh", ".aws", ".gnupg", ".docker", ".kube", ".config", ".azure", ".gcloud"}
)

# Container paths that must stay as the image built them. Bind-mounting over
# these is either a broken container or a way to smuggle in binaries/config that
# the rest of the hardening then runs.
_DENIED_CONTAINER_PATHS = frozenset(
    {"/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/sbin", "/sys", "/usr", "/var"}
)


class DockerSandboxError(RuntimeError):
    """Raised when the container cannot be created, reached, or configured."""


@dataclass(frozen=True)
class Mount:
    """One host directory (or file) exposed inside the container.

    Args:
        host_path: Path on the host. Relative paths resolve against the process
            CWD. Symlinks are resolved before validation, so a symlink pointing
            at a denied location is rejected on what it actually points to.
        container_path: Absolute path inside the container. This is the path the
            agent sees and the one to use in `read_file` / `execute`.
        read_only: When `True` (default) the container cannot modify the host
            files. Set `False` only for the directory results are written to.
        create: Create `host_path` as a directory if it does not exist. Without
            this, Docker would silently create a root-owned directory on Linux.
    """

    host_path: str | Path
    container_path: str
    read_only: bool = True
    create: bool = False

    def resolve(self, *, allow_unsafe: bool = False) -> tuple[Path, str]:
        """Validate both sides of the mount and return the resolved pair.

        Raises:
            DockerSandboxError: If either path is malformed or denied.
        """
        host = Path(self.host_path).expanduser()
        if self.create and not host.exists():
            host.mkdir(parents=True, exist_ok=True)
        if not host.exists():
            msg = f"Mount source does not exist: {host} (pass create=True to make it)"
            raise DockerSandboxError(msg)
        host = host.resolve()

        container = self.container_path
        if not container.startswith("/"):
            msg = f"Mount target must be an absolute container path, got {container!r}"
            raise DockerSandboxError(msg)
        container = os.path.normpath(container)
        if ".." in Path(container).parts:
            msg = f"Mount target must not contain '..': {self.container_path!r}"
            raise DockerSandboxError(msg)

        if allow_unsafe:
            return host, container

        if str(host) in _DENIED_HOST_PATHS:
            msg = f"Refusing to mount {host}: mounting a system root exposes the whole host to the agent."
            raise DockerSandboxError(msg)
        if host == Path.home():
            msg = f"Refusing to mount the home directory {host}: mount the specific subfolder instead."
            raise DockerSandboxError(msg)
        if host.name in _DENIED_HOST_NAMES:
            msg = f"Refusing to mount {host}: it holds credentials."
            raise DockerSandboxError(msg)
        if host.is_socket() or host.name.endswith(".sock"):
            msg = f"Refusing to mount the socket {host}: a mounted docker socket is a container escape."
            raise DockerSandboxError(msg)
        if container in _DENIED_CONTAINER_PATHS:
            msg = f"Refusing to mount over the container system path {container}."
            raise DockerSandboxError(msg)
        return host, container

    def to_flag(self, *, allow_unsafe: bool = False) -> str:
        """Render the `--mount` argument for `docker run`."""
        host, container = self.resolve(allow_unsafe=allow_unsafe)
        parts = ["type=bind", f"source={host}", f"target={container}"]
        if self.read_only:
            parts.append("readonly")
        return ",".join(parts)


@dataclass
class DockerSandboxBackend(BaseSandbox):
    """Deepagents backend whose shell and filesystem live in a Docker container.

    The container is started lazily on first use and torn down by `close()`, by
    the context manager, or at interpreter exit. All of `BaseSandbox`'s file
    operations (`ls`, `read`, `write`, `edit`, `grep`, `glob`, `delete`) are
    inherited and resolve inside the container, so the agent's view of "the
    filesystem" is exactly the image plus the declared mounts.

    Args:
        image: Image to run. Must already be present locally unless `pull=True`.
        mounts: Host paths exposed to the agent. Everything else on the host is
            invisible. This is the whole point of the backend.
        workdir: Working directory for `execute` and the anchor for relative
            work. Backed by tmpfs when `read_only_root` is set, so it is fast
            and vanishes with the container.
        network: Docker network mode. `"none"` (default) means the agent cannot
            reach the internet or your LAN. Use `"bridge"` only when a task
            genuinely needs to fetch something.
        read_only_root: Mount the image's filesystem read-only. Writes are then
            only possible under `workdir`, `/tmp`, and read-write mounts.
        memory / cpus / pids_limit: Resource caps, so a runaway command cannot
            take the host down.
        user: `"auto"` runs as the host uid:gid on Linux (keeps files written to
            a read-write mount owned by you) and as the image default elsewhere,
            where Docker Desktop already maps ownership. Pass an explicit
            `"uid:gid"` to override, or `None` for the image default.
        env: Environment variables inside the container. Nothing from the host
            environment leaks in unless listed here — do not put secrets here.
        timeout: Default per-command timeout in seconds.
        max_output_bytes: Cap on captured command output.
        max_file_bytes: Cap on a single upload/download, guarding the agent
            process's memory.
        pull: Pull `image` if it is not present locally.
        allow_unsafe_mounts: Disable the host/container path denylists. Only for
            deliberate, reviewed setups.
        enable_capture_offload: Let `FilesystemMiddleware` capture large command
            output to a file inside the container instead of routing it through
            the model. Safe on images with a POSIX shell and coreutils; left off
            by default because minimal images vary.
        container_name: Explicit container name. Defaults to a unique name.
        remove_on_exit: Remove the container when this object is closed.
        docker_bin: The CLI to drive. `"podman"` is API-compatible here.
        extra_run_args: Escape hatch appended verbatim to `docker run`.
    """

    image: str = DEFAULT_IMAGE
    mounts: list[Mount] = field(default_factory=list)
    workdir: str = DEFAULT_WORKDIR
    network: str = "none"
    read_only_root: bool = True
    workdir_tmpfs_size: str = "512m"
    memory: str | None = "2g"
    cpus: str | None = "2"
    pids_limit: int | None = 512
    user: str | None = "auto"
    env: dict[str, str] = field(default_factory=dict)
    timeout: int = DEFAULT_EXECUTE_TIMEOUT
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    pull: bool = False
    allow_unsafe_mounts: bool = False
    enable_capture_offload: bool = False
    container_name: str | None = None
    remove_on_exit: bool = True
    docker_bin: str = "docker"
    extra_run_args: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.timeout <= 0:
            msg = f"timeout must be positive, got {self.timeout}"
            raise ValueError(msg)
        if not self.workdir.startswith("/"):
            msg = f"workdir must be an absolute container path, got {self.workdir!r}"
            raise ValueError(msg)
        self._name = self.container_name or f"deepagents-sbx-{uuid.uuid4().hex[:10]}"
        self._started = False
        # Detected once the container is up: busybox and coreutils both ship
        # `timeout`, but a scratch-ish image may not, and wrapping with a
        # missing binary would fail every command.
        self._has_inner_timeout = False
        self._finalizer: weakref.finalize | None = None
        # Validate mounts now rather than at first use, so a bad path is a
        # startup error instead of a confusing failure mid-run.
        self._mount_flags = [m.to_flag(allow_unsafe=self.allow_unsafe_mounts) for m in self.mounts]

    # -- lifecycle ---------------------------------------------------------

    @property
    def id(self) -> str:
        """The container name, unique per backend instance."""
        return self._name

    def start(self) -> None:
        """Create and start the container if it is not already running.

        Idempotent; called automatically before the first command.
        """
        if self._started:
            return
        self._require_daemon()
        if self.pull and not self._image_present():
            pull = self._docker(["pull", self.image], timeout=_DOCKER_PULL_TIMEOUT)
            if pull.returncode != 0:
                msg = f"Failed to pull image {self.image!r}: {pull.stderr.strip()}"
                raise DockerSandboxError(msg)
        elif not self._image_present():
            msg = f"Image {self.image!r} is not present locally. Pass pull=True or `docker pull {self.image}` first."
            raise DockerSandboxError(msg)

        run = self._docker(self._build_run_args(), timeout=_DOCKER_CTL_TIMEOUT)
        if run.returncode != 0:
            msg = f"Failed to start sandbox container: {run.stderr.strip() or run.stdout.strip()}"
            raise DockerSandboxError(msg)

        state = self._docker(
            ["inspect", "-f", "{{.State.Running}}", self._name], timeout=_DOCKER_CTL_TIMEOUT
        )
        if state.stdout.strip() != "true":
            logs = self._docker(["logs", "--tail", "20", self._name], timeout=_DOCKER_CTL_TIMEOUT)
            self._force_remove()
            msg = f"Sandbox container exited immediately. Logs:\n{logs.stdout}{logs.stderr}"
            raise DockerSandboxError(msg)

        self._started = True
        if self.remove_on_exit:
            # weakref.finalize also registers with atexit, so an abandoned
            # backend does not leave a container running.
            self._finalizer = weakref.finalize(self, _remove_container, self.docker_bin, self._name)

        probe = self._exec_raw(["/bin/sh", "-c", "command -v timeout >/dev/null 2>&1"], timeout=30)
        self._has_inner_timeout = probe.returncode == 0

    def close(self) -> None:
        """Stop and remove the container."""
        if self._finalizer is not None:
            self._finalizer()
            self._finalizer = None
        elif self.remove_on_exit:
            self._force_remove()
        self._started = False

    def __enter__(self) -> DockerSandboxBackend:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- execution ---------------------------------------------------------

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Run a shell command inside the container.

        The command runs under `/bin/sh -c` with `workdir` as the working
        directory, under whatever network/resource/capability limits the
        container was created with. Stdout and stderr are combined, with stderr
        lines prefixed `[stderr]`.

        Args:
            command: Shell command string.
            timeout: Per-command timeout in seconds; falls back to the
                backend default.

        Returns:
            `ExecuteResponse` with combined output, exit code, and whether the
            output was truncated.
        """
        if not command or not isinstance(command, str):
            return ExecuteResponse(output="Error: Command must be a non-empty string.", exit_code=1)

        effective = timeout if timeout is not None else self.timeout
        if effective <= 0:
            msg = f"timeout must be positive, got {effective}"
            raise ValueError(msg)

        try:
            self.start()
        except DockerSandboxError as exc:
            return ExecuteResponse(output=f"Sandbox unavailable: {exc}", exit_code=1)

        # `docker exec` detaches the client on timeout but leaves the process
        # running in the container, so bound it on the inside too when the image
        # has `timeout`. The outer bound is the backstop for a stuck daemon.
        if self._has_inner_timeout:
            inner = ["/bin/sh", "-c", f"timeout -k 5 {effective} /bin/sh -c {shlex.quote(command)}"]
        else:
            inner = ["/bin/sh", "-c", command]

        try:
            result = self._exec_raw(inner, timeout=effective + 15)
        except subprocess.TimeoutExpired:
            self._kill_running_commands()
            return ExecuteResponse(
                output=f"Error: Command timed out after {effective} seconds. Re-run with a larger timeout if it needs longer.",
                exit_code=124,
            )

        if result.returncode == 124:
            return ExecuteResponse(
                output=f"Error: Command timed out after {effective} seconds. Re-run with a larger timeout if it needs longer.",
                exit_code=124,
            )

        parts: list[str] = []
        if result.stdout:
            parts.append(result.stdout)
        if result.stderr:
            parts.extend(f"[stderr] {line}" for line in result.stderr.strip().split("\n"))
        # Deliberately raw: no "<no output>" placeholder and no appended exit-code
        # line. `BaseSandbox` builds ls/read/grep/glob/edit on top of `execute`
        # and parses this string, so any decoration here is parsed as data — an
        # empty grep result would come back as a bogus error. The exit code
        # travels in `ExecuteResponse.exit_code`, and `FilesystemMiddleware`
        # already appends "[Command ... with exit code N]" for the model.
        output = "\n".join(parts)

        truncated = False
        if len(output) > self.max_output_bytes:
            output = output[: self.max_output_bytes]
            output += f"\n\n... Output truncated at {self.max_output_bytes} bytes."
            truncated = True

        return ExecuteResponse(output=output, exit_code=result.returncode, truncated=truncated)

    # -- file transfer -----------------------------------------------------

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Write files into the container, creating parent directories.

        Content is streamed to `cat` over `docker exec` stdin rather than copied
        with `docker cp`, so the bytes land owned by the container user and
        subject to its permissions — a write into a read-only mount fails
        instead of silently succeeding as root.

        Errors are reported per file; nothing raises.
        """
        responses: list[FileUploadResponse] = []
        for path, content in files:
            error = self._validate_container_path(path)
            if error is None and len(content) > self.max_file_bytes:
                error = f"file exceeds max_file_bytes ({len(content)} > {self.max_file_bytes})"
            if error is not None:
                responses.append(FileUploadResponse(path=path, error=error))
                continue
            try:
                self.start()
                script = 'mkdir -p -- "$(dirname -- "$1")" && cat > "$1"'
                result = self._exec_raw(
                    ["/bin/sh", "-c", script, "sh", path],
                    timeout=self.timeout,
                    input_bytes=content,
                    text=False,
                )
            except subprocess.TimeoutExpired:
                responses.append(FileUploadResponse(path=path, error="upload timed out"))
                continue
            except (DockerSandboxError, OSError) as exc:
                responses.append(FileUploadResponse(path=path, error=f"{type(exc).__name__}: {exc}"))
                continue
            if result.returncode == 0:
                responses.append(FileUploadResponse(path=path, error=None))
            else:
                stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
                responses.append(
                    FileUploadResponse(path=path, error=_classify_write_error(stderr))
                )
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Read files out of the container as bytes.

        Errors are reported per file; nothing raises.
        """
        # One `sh` probing then streaming keeps it to a single round-trip while
        # still distinguishing missing / directory / unreadable.
        script = (
            'p=$1; '
            '[ -e "$p" ] || exit 3; '
            '[ -d "$p" ] && exit 4; '
            '[ -r "$p" ] || exit 5; '
            'cat -- "$p"'
        )
        responses: list[FileDownloadResponse] = []
        for path in paths:
            error = self._validate_container_path(path)
            if error is not None:
                responses.append(FileDownloadResponse(path=path, error=error))
                continue
            try:
                self.start()
                result = self._exec_raw(
                    ["/bin/sh", "-c", script, "sh", path], timeout=self.timeout, text=False
                )
            except subprocess.TimeoutExpired:
                responses.append(FileDownloadResponse(path=path, error="download timed out"))
                continue
            except (DockerSandboxError, OSError) as exc:
                responses.append(
                    FileDownloadResponse(path=path, error=f"{type(exc).__name__}: {exc}")
                )
                continue

            if result.returncode == 3:
                responses.append(FileDownloadResponse(path=path, error=FILE_NOT_FOUND))
            elif result.returncode == 4:
                responses.append(FileDownloadResponse(path=path, error=IS_DIRECTORY))
            elif result.returncode == 5:
                responses.append(FileDownloadResponse(path=path, error=PERMISSION_DENIED))
            elif result.returncode != 0:
                stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
                responses.append(FileDownloadResponse(path=path, error=stderr or "download failed"))
            elif len(result.stdout) > self.max_file_bytes:
                responses.append(
                    FileDownloadResponse(
                        path=path,
                        error=f"file exceeds max_file_bytes ({len(result.stdout)} > {self.max_file_bytes})",
                    )
                )
            else:
                responses.append(FileDownloadResponse(path=path, content=result.stdout, error=None))
        return responses

    # -- internals ---------------------------------------------------------

    def _build_run_args(self) -> list[str]:
        args = [
            "run",
            "--detach",
            "--name",
            self._name,
            # Reap the zombies left by whatever the agent spawns.
            "--init",
            f"--network={self.network}",
            # A process cannot gain privileges it was not started with, so a
            # setuid binary inside the image is not a path back to root.
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--workdir",
            self.workdir,
        ]
        if self.read_only_root:
            args.append("--read-only")
            # With a read-only root the container still needs somewhere to work;
            # tmpfs keeps those writes off the host entirely. Mounted over a
            # directory the image already has, tmpfs comes up 0755 root, so an
            # image running as a non-root user could not write to its own
            # workdir; 1777 (sticky, like /tmp) works for whichever user runs.
            args += [
                "--mount",
                f"type=tmpfs,destination={self.workdir},tmpfs-size={self.workdir_tmpfs_size},tmpfs-mode=1777",
            ]
            args += ["--mount", "type=tmpfs,destination=/tmp,tmpfs-size=256m"]
        if self.memory:
            args += ["--memory", self.memory]
        if self.cpus:
            args += ["--cpus", self.cpus]
        if self.pids_limit:
            args += ["--pids-limit", str(self.pids_limit)]

        resolved_user = self._resolve_user()
        if resolved_user:
            args += ["--user", resolved_user]

        env = {"HOME": self.workdir, **self.env}
        for key, value in env.items():
            args += ["--env", f"{key}={value}"]

        for flag in self._mount_flags:
            args += ["--mount", flag]

        args += self.extra_run_args
        args.append(self.image)
        # PID 1 that never exits and costs nothing, so the container stays up
        # between `docker exec` calls.
        args += ["/bin/sh", "-c", "while :; do sleep 86400; done"]
        return args

    def _resolve_user(self) -> str | None:
        if self.user != "auto":
            return self.user
        # On Linux the container uid is the host uid for bind mounts, so running
        # as the caller keeps results written to a read-write mount owned by
        # them instead of by root. Docker Desktop (macOS/Windows) already maps
        # ownership through its filesystem layer, and forcing a uid there just
        # breaks images that expect their own user.
        if platform.system() == "Linux" and hasattr(os, "getuid"):
            return f"{os.getuid()}:{os.getgid()}"
        return None

    def _validate_container_path(self, path: str) -> str | None:
        """Return an error string if `path` is not a usable container path."""
        if not path or not isinstance(path, str):
            return "invalid_path"
        if not path.startswith("/"):
            return "invalid_path"
        if ".." in Path(path).parts:
            return "invalid_path"
        return None

    def _require_daemon(self) -> None:
        try:
            probe = self._docker(["version", "--format", "{{.Server.Version}}"], timeout=30)
        except FileNotFoundError as exc:
            msg = f"{self.docker_bin!r} is not on PATH. Install Docker (or pass docker_bin='podman')."
            raise DockerSandboxError(msg) from exc
        except subprocess.TimeoutExpired as exc:
            msg = "Timed out talking to the Docker daemon."
            raise DockerSandboxError(msg) from exc
        if probe.returncode != 0:
            msg = f"Docker daemon is not reachable: {probe.stderr.strip() or probe.stdout.strip()}"
            raise DockerSandboxError(msg)

    def _image_present(self) -> bool:
        probe = self._docker(["image", "inspect", self.image], timeout=_DOCKER_CTL_TIMEOUT)
        return probe.returncode == 0

    def _force_remove(self) -> None:
        _remove_container(self.docker_bin, self._name)

    def _kill_running_commands(self) -> None:
        """Best-effort cleanup after a client-side timeout.

        Restarting is heavier than killing one process tree but it is the only
        reliable way to stop whatever the abandoned `docker exec` left behind.
        """
        self._docker(["restart", "--time", "2", self._name], timeout=_DOCKER_CTL_TIMEOUT)

    def _exec_raw(
        self,
        argv: list[str],
        *,
        timeout: int,
        input_bytes: bytes | None = None,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run `docker exec` with `argv` as the in-container command."""
        cmd = [self.docker_bin, "exec"]
        if input_bytes is not None:
            cmd.append("--interactive")
        cmd += ["--workdir", self.workdir, self._name, *argv]
        # `input` and `stdin` are mutually exclusive in subprocess.run: feed the
        # payload when there is one, otherwise close stdin so a command that
        # reads it (python, cat) fails fast instead of hanging.
        stdin_kwargs: dict[str, object] = (
            {"input": input_bytes} if input_bytes is not None else {"stdin": subprocess.DEVNULL}
        )
        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=text,
            timeout=timeout,
            start_new_session=(sys.platform != "win32"),
            **stdin_kwargs,
        )

    def _docker(self, args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.docker_bin, *args],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            start_new_session=(sys.platform != "win32"),
        )


def _remove_container(docker_bin: str, name: str) -> None:
    """Remove `name`, ignoring the case where it is already gone."""
    try:
        subprocess.run(
            [docker_bin, "rm", "--force", "--volumes", name],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=_DOCKER_CTL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _classify_write_error(stderr: str) -> str:
    lowered = stderr.lower()
    if "read-only" in lowered or "permission denied" in lowered:
        return PERMISSION_DENIED
    if "is a directory" in lowered:
        return IS_DIRECTORY
    return stderr or "write failed"


__all__ = [
    "DEFAULT_IMAGE",
    "DEFAULT_WORKDIR",
    "DockerSandboxBackend",
    "DockerSandboxError",
    "Mount",
]
