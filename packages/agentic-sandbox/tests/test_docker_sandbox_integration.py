"""Integration tests: `DockerSandboxBackend` against a real Docker daemon.

Marked `integration` (skipped by the default `-m 'not integration'`). Run with:

    uv run pytest -m integration packages/agentic-sandbox/tests

Skipped automatically when no daemon is reachable. The image is pulled on first
use; override it with SANDBOX_TEST_IMAGE.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from deepagents.backends.protocol import FILE_NOT_FOUND, IS_DIRECTORY, PERMISSION_DENIED
from genai_agentic_sandbox.sandbox.docker import DEFAULT_IMAGE, DockerSandboxBackend, Mount

IMAGE = os.environ.get("SANDBOX_TEST_IMAGE", DEFAULT_IMAGE)


pytestmark = pytest.mark.integration


@pytest.fixture
def dirs(tmp_path):
    data = tmp_path / "device"
    data.mkdir()
    (data / "capture.txt").write_text("sensor-reading-42\n")
    out = tmp_path / "output"
    return data, out


@pytest.fixture
def sandbox(dirs):
    data, out = dirs
    backend = DockerSandboxBackend(
        image=IMAGE,
        pull=True,
        timeout=30,
        mounts=[
            Mount(data, "/mnt/device", read_only=True),
            Mount(out, "/mnt/output", read_only=False, create=True),
        ],
    )
    with backend:
        yield backend


def _container_exists(name: str) -> bool:
    probe = subprocess.run(["docker", "inspect", name], capture_output=True, check=False)
    return probe.returncode == 0


# -- execution ---------------------------------------------------------------------


def test_execute_returns_output_and_exit_code(sandbox):
    result = sandbox.execute("echo hello && exit 3")
    assert result.output == "hello\n"
    assert result.exit_code == 3


def test_stderr_is_prefixed(sandbox):
    result = sandbox.execute("echo oops >&2")
    assert result.output == "[stderr] oops"


def test_runs_in_workdir(sandbox):
    assert sandbox.execute("pwd").output.strip() == sandbox.workdir


def test_timeout_is_enforced(sandbox):
    result = sandbox.execute("sleep 20", timeout=2)
    assert result.exit_code == 124
    # The container is still usable afterwards.
    assert sandbox.execute("echo alive").output == "alive\n"


def test_host_env_does_not_leak(sandbox, monkeypatch):
    assert "OPENAI_API_KEY" not in sandbox.execute("env").output


# -- isolation -----------------------------------------------------------------------


def test_no_network(sandbox):
    result = sandbox.execute(
        "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)\""
    )
    assert result.exit_code != 0


def test_root_filesystem_is_read_only(sandbox):
    assert sandbox.execute("touch /usr/evil").exit_code != 0


def test_workdir_and_tmp_are_writable(sandbox):
    assert sandbox.execute("touch ./a /tmp/b && ls ./a /tmp/b").exit_code == 0


def test_all_capabilities_dropped(sandbox):
    result = sandbox.execute("grep CapEff /proc/self/status")
    assert result.output.split()[-1] == "0000000000000000"


def test_host_filesystem_is_not_visible(sandbox):
    assert sandbox.execute(f"ls {os.path.expanduser('~')}").exit_code != 0


# -- mounts & file transfer -------------------------------------------------------------


def test_read_only_mount_is_readable(sandbox):
    [resp] = sandbox.download_files(["/mnt/device/capture.txt"])
    assert resp.error is None
    assert resp.content == b"sensor-reading-42\n"


def test_read_only_mount_rejects_writes(sandbox, dirs):
    data, _ = dirs
    [resp] = sandbox.upload_files([("/mnt/device/capture.txt", b"tampered")])
    assert resp.error == PERMISSION_DENIED
    assert sandbox.execute("rm /mnt/device/capture.txt").exit_code != 0
    assert (data / "capture.txt").read_text() == "sensor-reading-42\n"


def test_writable_mount_lands_on_host(sandbox, dirs):
    _, out = dirs
    [resp] = sandbox.upload_files([("/mnt/output/reports/r.bin", b"\x00\x01done")])
    assert resp.error is None
    assert (out / "reports" / "r.bin").read_bytes() == b"\x00\x01done"


def test_upload_download_roundtrip_in_workdir(sandbox):
    payload = bytes(range(256))
    [up] = sandbox.upload_files([("/workspace/nested/blob.bin", payload)])
    [down] = sandbox.download_files(["/workspace/nested/blob.bin"])
    assert up.error is None
    assert down.content == payload


def test_download_errors(sandbox):
    missing, directory = sandbox.download_files(["/workspace/nope", "/workspace"])
    assert missing.error == FILE_NOT_FOUND
    assert directory.error == IS_DIRECTORY


# -- inherited BaseSandbox file operations -------------------------------------------------


def test_write_read_edit(sandbox):
    assert sandbox.write("/workspace/notes.txt", "alpha\nbeta\n").error is None
    edit = sandbox.edit("/workspace/notes.txt", "beta", "gamma")
    assert edit.error is None
    read = sandbox.read("/workspace/notes.txt")
    assert read.error is None
    assert "gamma" in read.file_data["content"]
    assert "beta" not in read.file_data["content"]


def test_ls_and_grep(sandbox):
    sandbox.write("/workspace/src/app.py", "def handler():\n    return 42\n")
    ls = sandbox.ls("/workspace/src")
    assert ls.error is None
    assert any(e["path"].endswith("app.py") for e in ls.entries)
    grep = sandbox.grep("return 42", path="/workspace")
    assert grep.error is None
    assert [m["line"] for m in grep.matches] == [2]


def test_grep_no_match_is_empty_not_error(sandbox):
    sandbox.write("/workspace/a.txt", "nothing here\n")
    grep = sandbox.grep("zzz-not-present", path="/workspace")
    assert grep.error is None
    assert grep.matches == []


# -- lifecycle --------------------------------------------------------------------------


def test_close_removes_container(dirs):
    backend = DockerSandboxBackend(image=IMAGE, pull=True)
    backend.start()
    assert _container_exists(backend.id)
    backend.close()
    assert not _container_exists(backend.id)
