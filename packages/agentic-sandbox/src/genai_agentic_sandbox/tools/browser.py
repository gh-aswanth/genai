"""Playwright MCP browser tools (same setup as `new_autoamtion _fix.py`), confined.

The browser runs on the HOST through `npx @playwright/mcp`, not in the sandbox
(the sandbox has no network by design). That makes its file access the one hole
in the sandbox, so it is closed here:

- The server's working directory is `<out>/.browser/captures`. Several tools
  (`browser_evaluate`, `browser_snapshot`, `browser_take_screenshot`, ...) take a
  `filename` and resolve it against that directory - not against wherever the
  CLI was started.
- `BrowserFileGuardMiddleware` (on job-search) rewrites every `filename` to a
  plain file name inside that folder, so absolute paths or `../` cannot write
  elsewhere on the host. The files appear in the sandbox at
  `/output/.browser/captures/<name>`.
- Tools that read host files or run arbitrary code on the host are not loaded
  at all: `browser_file_upload` / `browser_drop` (upload any host file to a
  website) and `browser_run_code_unsafe`.

Only the job-search subagent gets these tools.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import BaseTool

# A real Chrome identity: headless Chromium's default UA is a common block trigger.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Where browser-written files are visible inside the sandbox.
CAPTURES_IN_SANDBOX = "/output/.browser/captures"
# Tools that would read host files or run code on the host.
BLOCKED_BROWSER_TOOLS = frozenset(
    {"browser_file_upload", "browser_drop", "browser_run_code_unsafe"}
)


def playwright_server_config(
    profile_dir: str | Path, output_dir: str | Path, *, headless: bool = True
) -> dict[str, Any]:
    """MCP server config for `@playwright/mcp` (stdio), working in `output_dir`."""
    args = ["@playwright/mcp@latest"]
    if headless:
        args.append("--headless")
    args += [
        "--browser",
        "chromium",
        "--user-agent",
        USER_AGENT,
        "--viewport-size",
        "1440,900",
        # A saved profile so the browser looks like a normal returning user.
        "--user-data-dir",
        str(profile_dir),
        # Screenshots / snapshots land here, not in the process CWD.
        "--output-dir",
        str(output_dir),
        "--ignore-https-errors",
    ]
    # cwd = the captures folder: relative `filename`s resolve there, not in the
    # directory the CLI was started from.
    return {"command": "npx", "args": args, "transport": "stdio", "cwd": str(output_dir)}


def safe_capture_name(filename: str) -> str:
    """A plain file name (no directories) that stays inside the captures folder."""
    name = PurePosixPath(str(filename).replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".")
    return name or "capture.txt"


class BrowserFileGuardMiddleware(AgentMiddleware):
    """Keep browser tools from writing outside the captures folder.

    Any `filename` argument of a `browser_*` tool is replaced by a plain name, so
    it is written to the captures folder (the server's working directory). The
    tool result gets a note with the sandbox path to read it from.
    """

    def _rewrite(self, request: Any) -> tuple[Any, str | None]:
        call = request.tool_call
        args = call.get("args") or {}
        if not str(call.get("name", "")).startswith("browser_") or not args.get("filename"):
            return request, None
        safe = safe_capture_name(args["filename"])
        new_call = {**call, "args": {**args, "filename": safe}}
        return request.override(tool_call=new_call), safe

    @staticmethod
    def _note(result: Any, safe: str | None) -> Any:
        if safe is None or not hasattr(result, "content") or not isinstance(result.content, str):
            return result
        result.content += (
            f"\n[file saved as {safe}; read it in the sandbox at {CAPTURES_IN_SANDBOX}/{safe}]"
        )
        return result

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        request, safe = self._rewrite(request)
        return self._note(handler(request), safe)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        request, safe = self._rewrite(request)
        return self._note(await handler(request), safe)


@asynccontextmanager
async def playwright_browser_tools(
    profile_dir: str | Path, output_dir: str | Path, *, headless: bool = True
) -> AsyncIterator[list[BaseTool]]:
    """Yield the Playwright MCP tools bound to ONE long-lived browser session.

    `client.get_tools()` would open a fresh MCP session per tool call, so the
    browser would reset between navigate and evaluate and every page would look
    blank. Holding one session for the whole run keeps the page state. Tools in
    `BLOCKED_BROWSER_TOOLS` are dropped.
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_mcp_adapters.tools import load_mcp_tools

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    client = MultiServerMCPClient(
        {"playwright": playwright_server_config(profile_dir, output_dir, headless=headless)}
    )
    async with client.session("playwright") as session:
        tools = await load_mcp_tools(session)
        yield [t for t in tools if t.name not in BLOCKED_BROWSER_TOOLS]


__all__ = [
    "BLOCKED_BROWSER_TOOLS",
    "CAPTURES_IN_SANDBOX",
    "USER_AGENT",
    "BrowserFileGuardMiddleware",
    "playwright_browser_tools",
    "playwright_server_config",
    "safe_capture_name",
]
