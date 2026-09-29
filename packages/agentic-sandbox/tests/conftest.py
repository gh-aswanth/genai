from __future__ import annotations

import subprocess
from functools import cache

import pytest


@cache
def docker_daemon_available() -> bool:
    """True when the `docker` CLI can reach a daemon."""
    try:
        probe = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip `integration` tests when there is no Docker daemon to run them against."""
    integration = [item for item in items if item.get_closest_marker("integration")]
    if integration and not docker_daemon_available():
        skip = pytest.mark.skip(reason="Docker daemon not reachable")
        for item in integration:
            item.add_marker(skip)
