"""Unit tests for `DockerSandboxBackend` and `Mount`.

No Docker daemon needed: the backend's `_docker` / `_exec_raw` seams are replaced
with fakes, so these run anywhere and stay fast.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from deepagents.backends.protocol import FILE_NOT_FOUND, IS_DIRECTORY, PERMISSION_DENIED
from genai_agentic_sandbox.sandbox import docker as sbx
from genai_agentic_sandbox.sandbox.docker import (
    DEFAULT_WORKDIR,
    DockerSandboxBackend,
    DockerSandboxError,
    Mount,
)


def cp(
    returncode: int = 0, stdout: str | bytes = "", stderr: str | bytes = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeExec:
    """Stands in for `_exec_raw`: records every call and replays queued results."""

    def __init__(self, *results: subprocess.CompletedProcess | BaseException) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, argv, *, timeout, input_bytes=None, text=True):
        self.calls.append(
            {"argv": argv, "timeout": timeout, "input_bytes": input_bytes, "text": text}
        )
        result = self.results.pop(0) if self.results else cp()
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture(autouse=True)
def no_real_container_removal(monkeypatch):
    """Never shell out to `docker rm` from a unit test (including via atexit finalizers)."""
    removed: list[str] = []
    monkeypatch.setattr(sbx, "_remove_container", lambda _bin, name: removed.append(name))
    return removed


@pytest.fixture
def running_backend() -> DockerSandboxBackend:
    """A backend that believes its container is already up."""
    backend = DockerSandboxBackend(remove_on_exit=False)
    backend._started = True
    return backend


# -- Mount ---------------------------------------------------------------------


class TestMount:
    def test_missing_source_is_rejected(self, tmp_path):
        with pytest.raises(DockerSandboxError, match="does not exist"):
            Mount(tmp_path / "nope", "/mnt/x").resolve()

    def test_create_makes_the_source_directory(self, tmp_path):
        target = tmp_path / "out" / "nested"
        host, container = Mount(target, "/mnt/out", create=True).resolve()
        assert target.is_dir()
        assert host == target.resolve()
        assert container == "/mnt/out"

    def test_container_path_is_normalised(self, tmp_path):
        _, container = Mount(tmp_path, "/mnt//data/./x/").resolve()
        assert container == "/mnt/data/x"

    def test_relative_container_path_is_rejected(self, tmp_path):
        with pytest.raises(DockerSandboxError, match="absolute"):
            Mount(tmp_path, "mnt/data").resolve()

    @pytest.mark.parametrize("host", ["/", "/etc", "/usr", "/var"])
    def test_system_roots_are_denied(self, host):
        # On macOS /etc and /var resolve to /private/...; both forms are denied.
        with pytest.raises(DockerSandboxError, match="system root"):
            Mount(host, "/mnt/host").resolve()

    def test_home_directory_is_denied(self):
        with pytest.raises(DockerSandboxError, match="home directory"):
            Mount(Path.home(), "/mnt/home").resolve()

    @pytest.mark.parametrize("name", [".ssh", ".aws", ".docker", ".kube"])
    def test_credential_directories_are_denied(self, tmp_path, name):
        creds = tmp_path / name
        creds.mkdir()
        with pytest.raises(DockerSandboxError, match="credentials"):
            Mount(creds, "/mnt/creds").resolve()

    def test_socket_files_are_denied(self, tmp_path):
        sock = tmp_path / "docker.sock"
        sock.touch()
        with pytest.raises(DockerSandboxError, match="socket"):
            Mount(sock, "/var/run/docker.sock").resolve()

    @pytest.mark.parametrize("target", ["/", "/etc", "/usr", "/bin", "/proc"])
    def test_container_system_paths_are_denied(self, tmp_path, target):
        with pytest.raises(DockerSandboxError, match="container system path"):
            Mount(tmp_path, target).resolve()

    def test_symlink_is_judged_by_its_target(self, tmp_path):
        creds = tmp_path / ".aws"
        creds.mkdir()
        link = tmp_path / "innocent"
        link.symlink_to(creds)
        with pytest.raises(DockerSandboxError, match="credentials"):
            Mount(link, "/mnt/x").resolve()

    def test_allow_unsafe_bypasses_denylists(self, tmp_path):
        creds = tmp_path / ".ssh"
        creds.mkdir()
        host, container = Mount(creds, "/etc").resolve(allow_unsafe=True)
        assert host == creds.resolve()
        assert container == "/etc"

    def test_to_flag_read_only(self, tmp_path):
        flag = Mount(tmp_path, "/mnt/in").to_flag()
        assert flag == f"type=bind,source={tmp_path.resolve()},target=/mnt/in,readonly"

    def test_to_flag_read_write(self, tmp_path):
        flag = Mount(tmp_path, "/mnt/out", read_only=False).to_flag()
        assert flag.endswith("target=/mnt/out")
        assert "readonly" not in flag


# -- construction ----------------------------------------------------------------


class TestConstruction:
    def test_defaults_are_restrictive(self):
        backend = DockerSandboxBackend()
        assert backend.network == "none"
        assert backend.read_only_root is True
        assert backend.workdir == DEFAULT_WORKDIR
        assert backend.memory and backend.cpus and backend.pids_limit

    def test_id_is_unique_per_instance(self):
        a, b = DockerSandboxBackend(), DockerSandboxBackend()
        assert a.id != b.id
        assert a.id.startswith("deepagents-sbx-")

    def test_explicit_container_name(self):
        assert DockerSandboxBackend(container_name="my-box").id == "my-box"

    @pytest.mark.parametrize("timeout", [0, -5])
    def test_non_positive_timeout_is_rejected(self, timeout):
        with pytest.raises(ValueError, match="timeout"):
            DockerSandboxBackend(timeout=timeout)

    def test_relative_workdir_is_rejected(self):
        with pytest.raises(ValueError, match="workdir"):
            DockerSandboxBackend(workdir="workspace")

    def test_bad_mount_fails_at_construction(self):
        with pytest.raises(DockerSandboxError):
            DockerSandboxBackend(mounts=[Mount("/etc", "/mnt/etc")])


# -- docker run arguments ----------------------------------------------------------


class TestRunArgs:
    def test_hardening_flags(self):
        args = DockerSandboxBackend(user=None)._build_run_args()
        assert "--network=none" in args
        assert "--read-only" in args
        assert "--init" in args
        assert args[args.index("--cap-drop") + 1] == "ALL"
        assert args[args.index("--security-opt") + 1] == "no-new-privileges"
        assert args[args.index("--memory") + 1] == "2g"
        assert args[args.index("--cpus") + 1] == "2"
        assert args[args.index("--pids-limit") + 1] == "512"
        assert "--user" not in args

    def test_tmpfs_for_workdir_and_tmp_when_root_is_read_only(self):
        args = DockerSandboxBackend(workdir="/work", workdir_tmpfs_size="64m")._build_run_args()
        assert "type=tmpfs,destination=/work,tmpfs-size=64m,tmpfs-mode=1777" in args
        assert "type=tmpfs,destination=/tmp,tmpfs-size=256m" in args

    def test_writable_root_has_no_tmpfs(self):
        args = DockerSandboxBackend(read_only_root=False)._build_run_args()
        assert "--read-only" not in args
        assert not any(a.startswith("type=tmpfs") for a in args)

    def test_limits_can_be_disabled(self):
        args = DockerSandboxBackend(memory=None, cpus=None, pids_limit=None)._build_run_args()
        assert "--memory" not in args and "--cpus" not in args and "--pids-limit" not in args

    def test_env_sets_home_and_user_values_win(self):
        args = DockerSandboxBackend(env={"FOO": "bar", "HOME": "/custom"})._build_run_args()
        envs = [args[i + 1] for i, a in enumerate(args) if a == "--env"]
        assert "FOO=bar" in envs
        assert "HOME=/custom" in envs
        assert f"HOME={DEFAULT_WORKDIR}" not in envs

    def test_host_environment_does_not_leak(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        args = DockerSandboxBackend()._build_run_args()
        assert not any("sk-secret" in a for a in args)

    def test_mounts_and_image_and_keepalive_command_order(self, tmp_path):
        backend = DockerSandboxBackend(
            image="alpine:3", mounts=[Mount(tmp_path, "/mnt/in")], extra_run_args=["--label", "x=1"]
        )
        args = backend._build_run_args()
        assert f"type=bind,source={tmp_path.resolve()},target=/mnt/in,readonly" in args
        image_at = args.index("alpine:3")
        assert args[image_at - 2 : image_at] == ["--label", "x=1"]
        assert args[image_at + 1 : image_at + 3] == ["/bin/sh", "-c"]
        assert args[:4] == ["run", "--detach", "--name", backend.id]

    def test_explicit_user(self):
        args = DockerSandboxBackend(user="1000:1000")._build_run_args()
        assert args[args.index("--user") + 1] == "1000:1000"

    def test_auto_user_on_linux_uses_host_uid(self, monkeypatch):
        monkeypatch.setattr(sbx.platform, "system", lambda: "Linux")
        monkeypatch.setattr(sbx.os, "getuid", lambda: 1234, raising=False)
        monkeypatch.setattr(sbx.os, "getgid", lambda: 99, raising=False)
        assert DockerSandboxBackend()._resolve_user() == "1234:99"

    def test_auto_user_elsewhere_uses_image_default(self, monkeypatch):
        monkeypatch.setattr(sbx.platform, "system", lambda: "Darwin")
        assert DockerSandboxBackend()._resolve_user() is None


# -- lifecycle ---------------------------------------------------------------------


class FakeDocker:
    """Stands in for `_docker`, answering by sub-command."""

    def __init__(self, **overrides: subprocess.CompletedProcess) -> None:
        self.responses = {
            "version": cp(0, "27.0.0"),
            "image": cp(0),
            "pull": cp(0),
            "run": cp(0, "containerid"),
            "inspect": cp(0, "true\n"),
            "logs": cp(0, "boom"),
            "restart": cp(0),
            **overrides,
        }
        self.calls: list[list[str]] = []

    def __call__(self, args, *, timeout):
        self.calls.append(args)
        return self.responses[args[0]]

    def commands(self) -> list[str]:
        return [c[0] for c in self.calls]


class TestLifecycle:
    def make(self, monkeypatch, docker: FakeDocker, **kwargs) -> DockerSandboxBackend:
        backend = DockerSandboxBackend(**kwargs)
        monkeypatch.setattr(backend, "_docker", docker)
        monkeypatch.setattr(backend, "_exec_raw", FakeExec(cp(0)))  # `timeout` probe
        return backend

    def test_start_runs_container_and_probes_timeout(self, monkeypatch):
        docker = FakeDocker()
        backend = self.make(monkeypatch, docker)
        backend.start()
        assert docker.commands() == ["version", "image", "run", "inspect"]
        assert backend._started is True
        assert backend._has_inner_timeout is True

    def test_start_is_idempotent(self, monkeypatch):
        docker = FakeDocker()
        backend = self.make(monkeypatch, docker)
        backend.start()
        backend.start()
        assert docker.commands().count("run") == 1

    def test_missing_timeout_binary_is_detected(self, monkeypatch):
        backend = self.make(monkeypatch, FakeDocker())
        monkeypatch.setattr(backend, "_exec_raw", FakeExec(cp(1)))
        backend.start()
        assert backend._has_inner_timeout is False

    def test_missing_docker_cli(self):
        backend = DockerSandboxBackend(docker_bin="definitely-not-a-docker-binary")
        with pytest.raises(DockerSandboxError, match="not on PATH"):
            backend.start()

    def test_unreachable_daemon(self, monkeypatch):
        backend = self.make(monkeypatch, FakeDocker(version=cp(1, "", "Cannot connect")))
        with pytest.raises(DockerSandboxError, match="not reachable.*Cannot connect"):
            backend.start()

    def test_missing_image_without_pull(self, monkeypatch):
        docker = FakeDocker(image=cp(1))
        backend = self.make(monkeypatch, docker)
        with pytest.raises(DockerSandboxError, match="not present locally"):
            backend.start()
        assert "run" not in docker.commands()

    def test_missing_image_is_pulled(self, monkeypatch):
        docker = FakeDocker(image=cp(1))
        backend = self.make(monkeypatch, docker, pull=True)
        backend.start()
        assert "pull" in docker.commands()

    def test_pull_failure(self, monkeypatch):
        backend = self.make(
            monkeypatch, FakeDocker(image=cp(1), pull=cp(1, "", "denied")), pull=True
        )
        with pytest.raises(DockerSandboxError, match="Failed to pull.*denied"):
            backend.start()

    def test_run_failure(self, monkeypatch):
        backend = self.make(monkeypatch, FakeDocker(run=cp(125, "", "bad flag")))
        with pytest.raises(DockerSandboxError, match="Failed to start.*bad flag"):
            backend.start()

    def test_container_exiting_immediately_is_removed(self, monkeypatch, no_real_container_removal):
        backend = self.make(monkeypatch, FakeDocker(inspect=cp(0, "false\n")))
        with pytest.raises(DockerSandboxError, match="exited immediately"):
            backend.start()
        assert no_real_container_removal == [backend.id]
        assert backend._started is False

    def test_close_is_safe_to_call_twice(self, monkeypatch, no_real_container_removal):
        backend = self.make(monkeypatch, FakeDocker())
        backend.start()
        backend.close()
        backend.close()
        assert no_real_container_removal.count(backend.id) == 2  # finalizer, then force-remove
        assert backend._started is False

    def test_close_keeps_container_when_asked(self, monkeypatch, no_real_container_removal):
        backend = self.make(monkeypatch, FakeDocker(), remove_on_exit=False)
        backend.start()
        backend.close()
        assert no_real_container_removal == []

    def test_context_manager(self, monkeypatch, no_real_container_removal):
        backend = self.make(monkeypatch, FakeDocker())
        with backend as b:
            assert b is backend and backend._started
        assert backend.id in no_real_container_removal


# -- execute -----------------------------------------------------------------------


class TestExecute:
    def test_empty_command(self, running_backend):
        result = running_backend.execute("")
        assert result.exit_code == 1
        assert "non-empty" in result.output

    def test_non_positive_timeout_raises(self, running_backend):
        with pytest.raises(ValueError):
            running_backend.execute("ls", timeout=0)

    def test_sandbox_unavailable_is_reported_not_raised(self):
        backend = DockerSandboxBackend(docker_bin="definitely-not-a-docker-binary")
        result = backend.execute("ls")
        assert result.exit_code == 1
        assert result.output.startswith("Sandbox unavailable")

    def test_wraps_with_inner_timeout_when_available(self, running_backend, monkeypatch):
        fake = FakeExec(cp(0, "ok\n"))
        monkeypatch.setattr(running_backend, "_exec_raw", fake)
        running_backend._has_inner_timeout = True
        running_backend.execute("echo 'hi there'", timeout=7)
        argv = fake.calls[0]["argv"]
        assert argv[:2] == ["/bin/sh", "-c"]
        assert argv[2] == "timeout -k 5 7 /bin/sh -c 'echo '\"'\"'hi there'\"'\"''"
        assert fake.calls[0]["timeout"] == 7 + 15

    def test_runs_plain_when_no_inner_timeout(self, running_backend, monkeypatch):
        fake = FakeExec(cp(0))
        monkeypatch.setattr(running_backend, "_exec_raw", fake)
        running_backend.execute("ls -la")
        assert fake.calls[0]["argv"] == ["/bin/sh", "-c", "ls -la"]
        assert fake.calls[0]["timeout"] == running_backend.timeout + 15

    def test_combines_stdout_and_prefixed_stderr(self, running_backend, monkeypatch):
        monkeypatch.setattr(running_backend, "_exec_raw", FakeExec(cp(2, "out\n", "e1\ne2\n")))
        result = running_backend.execute("x")
        assert result.output == "out\n\n[stderr] e1\n[stderr] e2"
        assert result.exit_code == 2
        assert result.truncated is False

    def test_empty_output_is_not_decorated(self, running_backend, monkeypatch):
        # BaseSandbox parses this string; a placeholder would be read as data.
        monkeypatch.setattr(running_backend, "_exec_raw", FakeExec(cp(1)))
        assert running_backend.execute("grep nothing").output == ""

    def test_inner_timeout_exit_code(self, running_backend, monkeypatch):
        monkeypatch.setattr(running_backend, "_exec_raw", FakeExec(cp(124)))
        result = running_backend.execute("sleep 99", timeout=3)
        assert result.exit_code == 124
        assert "timed out after 3 seconds" in result.output

    def test_client_timeout_restarts_container(self, running_backend, monkeypatch):
        docker = FakeDocker()
        monkeypatch.setattr(running_backend, "_docker", docker)
        monkeypatch.setattr(
            running_backend,
            "_exec_raw",
            FakeExec(subprocess.TimeoutExpired(cmd="docker", timeout=1)),
        )
        result = running_backend.execute("sleep 99", timeout=1)
        assert result.exit_code == 124
        assert docker.calls == [["restart", "--time", "2", running_backend.id]]

    def test_output_is_truncated(self, running_backend, monkeypatch):
        running_backend.max_output_bytes = 10
        monkeypatch.setattr(running_backend, "_exec_raw", FakeExec(cp(0, "x" * 50)))
        result = running_backend.execute("yes")
        assert result.truncated is True
        assert result.output.startswith("x" * 10 + "\n\n... Output truncated at 10 bytes.")


# -- file transfer -------------------------------------------------------------------


class TestUpload:
    @pytest.mark.parametrize("path", ["", "relative.txt", "/workspace/../etc/passwd"])
    def test_invalid_paths(self, running_backend, path):
        [resp] = running_backend.upload_files([(path, b"x")])
        assert resp.error == "invalid_path"

    def test_oversized_file(self, running_backend):
        running_backend.max_file_bytes = 3
        [resp] = running_backend.upload_files([("/workspace/a", b"1234")])
        assert "max_file_bytes" in resp.error

    def test_streams_content_over_stdin(self, running_backend, monkeypatch):
        fake = FakeExec(cp(0, b"", b""))
        monkeypatch.setattr(running_backend, "_exec_raw", fake)
        [resp] = running_backend.upload_files([("/workspace/dir/a.txt", b"hello")])
        assert resp.error is None
        call = fake.calls[0]
        assert call["input_bytes"] == b"hello"
        assert call["text"] is False
        # Path is passed as a positional arg, never interpolated into the script.
        assert call["argv"][-1] == "/workspace/dir/a.txt"
        assert "/workspace/dir/a.txt" not in call["argv"][2]

    @pytest.mark.parametrize(
        ("stderr", "expected"),
        [
            (b"sh: can't create /mnt/in/a: Read-only file system", PERMISSION_DENIED),
            (b"sh: /root/a: Permission denied", PERMISSION_DENIED),
            (b"sh: /workspace: Is a directory", IS_DIRECTORY),
            (b"something odd", "something odd"),
            (b"", "write failed"),
        ],
    )
    def test_error_classification(self, running_backend, monkeypatch, stderr, expected):
        monkeypatch.setattr(running_backend, "_exec_raw", FakeExec(cp(1, b"", stderr)))
        [resp] = running_backend.upload_files([("/mnt/in/a", b"x")])
        assert resp.error == expected

    def test_timeout_is_reported_per_file(self, running_backend, monkeypatch):
        fake = FakeExec(subprocess.TimeoutExpired(cmd="docker", timeout=1), cp(0, b"", b""))
        monkeypatch.setattr(running_backend, "_exec_raw", fake)
        first, second = running_backend.upload_files(
            [("/workspace/a", b"1"), ("/workspace/b", b"2")]
        )
        assert first.error == "upload timed out"
        assert second.error is None


class TestDownload:
    @pytest.mark.parametrize(
        ("returncode", "expected"),
        [(3, FILE_NOT_FOUND), (4, IS_DIRECTORY), (5, PERMISSION_DENIED)],
    )
    def test_probe_exit_codes(self, running_backend, monkeypatch, returncode, expected):
        monkeypatch.setattr(running_backend, "_exec_raw", FakeExec(cp(returncode, b"", b"")))
        [resp] = running_backend.download_files(["/workspace/a"])
        assert resp.error == expected
        assert resp.content is None

    def test_other_failure_uses_stderr(self, running_backend, monkeypatch):
        monkeypatch.setattr(running_backend, "_exec_raw", FakeExec(cp(1, b"", b"I/O error")))
        [resp] = running_backend.download_files(["/workspace/a"])
        assert resp.error == "I/O error"

    def test_success(self, running_backend, monkeypatch):
        monkeypatch.setattr(running_backend, "_exec_raw", FakeExec(cp(0, b"\x00bytes\xff", b"")))
        [resp] = running_backend.download_files(["/workspace/a.bin"])
        assert resp.error is None
        assert resp.content == b"\x00bytes\xff"

    def test_oversized_file(self, running_backend, monkeypatch):
        running_backend.max_file_bytes = 2
        monkeypatch.setattr(running_backend, "_exec_raw", FakeExec(cp(0, b"abc", b"")))
        [resp] = running_backend.download_files(["/workspace/a"])
        assert "max_file_bytes" in resp.error

    def test_invalid_path(self, running_backend):
        [resp] = running_backend.download_files(["../etc/passwd"])
        assert resp.error == "invalid_path"
