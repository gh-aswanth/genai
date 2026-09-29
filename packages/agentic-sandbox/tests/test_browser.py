"""The host-side browser is confined: files only in its captures folder, no host-file tools."""

from __future__ import annotations

import asyncio
import shutil
from unittest import mock

import pytest
from genai_agentic_sandbox.agent import declarative_specs
from genai_agentic_sandbox.sandbox.docker import DockerSandboxBackend
from genai_agentic_sandbox.tools import browser
from genai_agentic_sandbox.tools.browser import (
    BLOCKED_BROWSER_TOOLS,
    CAPTURES_IN_SANDBOX,
    BrowserFileGuardMiddleware,
    playwright_server_config,
    safe_capture_name,
)
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI


class Req:
    def __init__(self, name, args):
        self.tool_call = {"name": name, "args": args, "id": "c1"}

    def override(self, tool_call):
        return Req(tool_call["name"], tool_call["args"])


def run(name, args):
    seen = []

    def handler(r):
        seen.append(r.tool_call["args"])
        return ToolMessage(content="ok", tool_call_id="c1")

    result = BrowserFileGuardMiddleware().wrap_tool_call(Req(name, args), handler)
    return seen[0], result


@pytest.mark.parametrize(
    ("given", "safe"),
    [
        ("listings_extract.json", "listings_extract.json"),
        ("/Users/someone/Desktop/x.json", "x.json"),
        ("../../infopark_candidates.json", "infopark_candidates.json"),
        ("C:\\temp\\y.png", "y.png"),
        (".env", "env"),
        ("a b/c?.json", "c_.json"),
        ("", "capture.txt"),
    ],
)
def test_safe_capture_name(given, safe):
    assert safe_capture_name(given) == safe


def test_filename_is_forced_into_the_captures_folder():
    args, result = run(
        "browser_evaluate", {"function": "() => 1", "filename": "/Users/me/.ssh/out.json"}
    )
    assert args == {"function": "() => 1", "filename": "out.json"}
    assert f"{CAPTURES_IN_SANDBOX}/out.json" in result.content


def test_calls_without_filename_are_untouched():
    args, result = run("browser_evaluate", {"function": "() => 1"})
    assert args == {"function": "() => 1"} and result.content == "ok"
    args, _ = run("write_file", {"file_path": "/output/jobs/x.json", "filename": "/etc/x"})
    assert args["filename"] == "/etc/x"  # only browser_* tools are rewritten


def test_async_path():
    async def handler(r):
        return ToolMessage(content=str(r.tool_call["args"]["filename"]), tool_call_id="c1")

    result = asyncio.run(
        BrowserFileGuardMiddleware().awrap_tool_call(
            Req("browser_snapshot", {"filename": "../s.md"}), handler
        )
    )
    assert result.content.startswith("s.md")


def test_server_runs_in_the_captures_folder(tmp_path):
    cfg = playwright_server_config(tmp_path / "profile", tmp_path / "captures")
    assert cfg["cwd"] == str(tmp_path / "captures")
    assert cfg["args"][cfg["args"].index("--output-dir") + 1] == str(tmp_path / "captures")


def test_host_file_tools_are_dropped(tmp_path):
    @tool
    def browser_navigate(url: str) -> str:
        """nav"""
        return url

    fakes = [browser_navigate] + [
        tool(name, description="x")(lambda **kw: "") for name in sorted(BLOCKED_BROWSER_TOOLS)
    ]

    class FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def session(self, name):
            return FakeSession()

    async def load(session):
        return fakes

    async def go():
        with (
            mock.patch("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient),
            mock.patch("langchain_mcp_adapters.tools.load_mcp_tools", load),
        ):
            async with browser.playwright_browser_tools(tmp_path / "p", tmp_path / "c") as tools:
                return [t.name for t in tools]

    assert asyncio.run(go()) == ["browser_navigate"]


def test_only_job_search_has_the_guard():
    subagents = {
        s["name"]: s
        for s in declarative_specs(
            ChatOpenAI(model="gpt-5.5", api_key="x"), DockerSandboxBackend(), []
        ).values()
    }
    guarded = {
        n
        for n, s in subagents.items()
        if any(isinstance(m, BrowserFileGuardMiddleware) for m in s["middleware"])
    }
    assert guarded == {"job-search"}


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("npx") is None, reason="npx not installed")
def test_real_browser_cannot_write_outside_captures(tmp_path, monkeypatch):
    """Real Playwright MCP: an absolute filename outside the captures folder is confined."""
    captures, launch_dir, target = (
        tmp_path / "captures",
        tmp_path / "launched-here",
        tmp_path / "escape",
    )
    launch_dir.mkdir()
    target.mkdir()
    monkeypatch.chdir(launch_dir)  # where the CLI was started (stray files used to land here)
    guard = BrowserFileGuardMiddleware()

    async def go():
        async with browser.playwright_browser_tools(tmp_path / "profile", captures) as tools:
            by = {t.name: t for t in tools}
            assert not BLOCKED_BROWSER_TOOLS & set(by)
            await by["browser_navigate"].ainvoke({"url": "data:text/html,<title>hi</title>"})

            async def handler(r):
                return ToolMessage(
                    content=str(await by[r.tool_call["name"]].ainvoke(r.tool_call["args"])),
                    tool_call_id="c1",
                )

            for filename in (str(target / "abs.json"), "relative.json"):
                req = Req(
                    "browser_evaluate", {"function": "() => document.title", "filename": filename}
                )
                await guard.awrap_tool_call(req, handler)

    asyncio.run(go())
    assert sorted(p.name for p in captures.glob("*.json")) == ["abs.json", "relative.json"]
    assert list(target.iterdir()) == [] and list(launch_dir.iterdir()) == []
