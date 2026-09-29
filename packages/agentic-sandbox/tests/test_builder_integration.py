"""Integration tests: build real sandbox images and run them as the sandbox.

Marked `integration` and skipped without a Docker daemon. Run with:

    uv run pytest -m integration packages/agentic-sandbox/tests/test_builder_integration.py
"""

from __future__ import annotations

import subprocess

import pytest
from genai_agentic_sandbox.builder import SandboxImageBuilder

pytestmark = pytest.mark.integration

TEST_REPOSITORY = "genai-agentic-sandbox-test"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Build a small image from a throwaway pyproject; remove it afterwards."""
    path = tmp_path_factory.mktemp("proj") / "pyproject.toml"
    path.write_text('[project]\nname = "t"\nversion = "0"\ndependencies = ["tabulate==0.9.0"]\n')
    builder = SandboxImageBuilder(pyproject=path, repository=TEST_REPOSITORY)
    builder.build()
    yield builder
    subprocess.run(
        ["docker", "rmi", "--force", builder.tag, f"{TEST_REPOSITORY}:latest"],
        capture_output=True,
        check=False,
    )


def test_build_creates_tagged_image(built):
    assert built.exists()


def test_second_build_is_a_noop(built, monkeypatch):
    calls = []
    original = built._docker
    monkeypatch.setattr(
        built, "_docker", lambda args, **kw: calls.append(args) or original(args, **kw)
    )
    built.build()
    assert all(c[0] != "build" for c in calls)


def test_image_runs_as_sandbox(built):
    with built.backend() as sandbox:
        assert sandbox.execute("whoami").output.strip() == "sandbox"
        assert sandbox.execute("which python").output.strip() == "/opt/venv/bin/python"
        assert (
            sandbox.execute(
                "python -c 'import sys; print(sys.version_info[:2] >= (3, 14))'"
            ).output.strip()
            == "True"
        )


def test_pyproject_dependencies_are_installed(built):
    with built.backend() as sandbox:
        result = sandbox.execute("python -c 'import tabulate; print(tabulate.__version__)'")
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "0.9.0"


def test_non_root_user_can_use_workdir(built):
    with built.backend() as sandbox:
        assert sandbox.execute("touch ./a && echo hi > /tmp/b && ls ./a").exit_code == 0
        assert sandbox.write("/workspace/notes.txt", "hello\n").error is None
        assert "hello" in sandbox.read("/workspace/notes.txt").file_data["content"]


def test_hardening_still_applies(built):
    with built.backend() as sandbox:
        assert sandbox.execute("touch /opt/venv/evil").exit_code != 0
        assert sandbox.execute("uv pip install requests").exit_code != 0  # no network


def test_default_image_has_default_dependencies():
    """The shipped sandbox.pyproject.toml builds and its packages import."""
    builder = SandboxImageBuilder()
    with builder.backend() as sandbox:
        result = sandbox.execute("python -c 'import numpy, pandas, matplotlib'")
        assert result.exit_code == 0, result.output


# -- base image that already ships packages ----------------------------------------------

BASE_PANDAS = "2.3.3"
SYSTEM_PANDAS_BASE = "genai-agentic-sandbox-test-base:system-pandas"
LEAKY_IMAGE = "genai-agentic-sandbox-test-base:leaky-venv"


def _docker_build(tag: str, dockerfile: str) -> None:
    result = subprocess.run(
        ["docker", "build", "--tag", tag, "-"],
        input=dockerfile,
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    assert result.returncode == 0, result.stderr[-2000:]


@pytest.fixture(scope="module")
def system_pandas_base():
    """A base image with an old pandas installed into the *system* site-packages."""
    _docker_build(
        SYSTEM_PANDAS_BASE,
        f"FROM {SandboxImageBuilder().base_image}\n"
        f"RUN pip install --no-cache-dir --root-user-action=ignore pandas=={BASE_PANDAS}\n",
    )
    yield SYSTEM_PANDAS_BASE
    subprocess.run(
        ["docker", "rmi", "--force", SYSTEM_PANDAS_BASE], capture_output=True, check=False
    )


@pytest.fixture(scope="module")
def pandas_pyproject(tmp_path_factory):
    path = tmp_path_factory.mktemp("pandas") / "pyproject.toml"
    path.write_text('[project]\nname = "p"\nversion = "0"\ndependencies = ["pandas>=3"]\n')
    return path


@pytest.fixture(scope="module")
def pandas_over_system_pandas(system_pandas_base, pandas_pyproject):
    builder = SandboxImageBuilder(
        pyproject=pandas_pyproject, base_image=system_pandas_base, repository=TEST_REPOSITORY
    )
    builder.build()  # verify=True: raises if the venv pandas is missing or wrong
    yield builder
    subprocess.run(
        ["docker", "rmi", "--force", builder.tag, f"{TEST_REPOSITORY}:latest"],
        capture_output=True,
        check=False,
    )


def test_base_image_really_ships_old_pandas(system_pandas_base):
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            system_pandas_base,
            "python3",
            "-c",
            "import pandas; print(pandas.__version__)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.strip() == BASE_PANDAS


def test_verify_reports_venv_pandas_not_base_pandas(pandas_over_system_pandas):
    report = pandas_over_system_pandas.verify()
    assert report.ok, report.summary()
    [pandas] = report.packages
    assert pandas.installed_version.startswith("3.")
    assert pandas.location.startswith("/opt/venv/")


def test_sandbox_imports_pyproject_pandas_not_base_pandas(pandas_over_system_pandas):
    with pandas_over_system_pandas.backend() as sandbox:
        result = sandbox.execute(
            "python -c 'import pandas; print(pandas.__version__, pandas.__file__)'"
        )
        version, location = result.output.split()
        assert version.startswith("3."), result.output
        assert location.startswith("/opt/venv/")
        # The old copy is still in the image, just not what the sandbox's python sees.
        base = sandbox.execute(
            "/usr/local/bin/python3 -c 'import pandas; print(pandas.__version__)'"
        )
        assert base.output.strip() == BASE_PANDAS


def test_verify_rejects_pandas_leaking_in_from_system_site_packages(
    system_pandas_base, pandas_pyproject
):
    # A venv created with --system-site-packages "has" pandas, but only the base image's copy.
    _docker_build(
        LEAKY_IMAGE,
        f"FROM {system_pandas_base}\n"
        "RUN uv venv --system-site-packages /opt/venv --python /usr/local/bin/python3\n"
        "ENV PATH=/opt/venv/bin:$PATH\n",
    )
    try:
        report = SandboxImageBuilder(pyproject=pandas_pyproject).verify(LEAKY_IMAGE)
    finally:
        subprocess.run(["docker", "rmi", "--force", LEAKY_IMAGE], capture_output=True, check=False)
    assert not report.ok
    [pandas] = report.packages
    assert pandas.installed_version == BASE_PANDAS
    assert "outside the venv" in pandas.problem


def test_verify_rejects_wrong_version_and_missing_package(built, tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\nname = "x"\nversion = "0"\ndependencies = ["tabulate<0.9", "rich"]\n'
    )
    report = SandboxImageBuilder(pyproject=path).verify(built.tag)
    problems = {p.name: p.problem for p in report.packages}
    assert "does not satisfy <0.9" in problems["tabulate"]
    assert problems["rich"] == "not installed"
