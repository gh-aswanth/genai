"""Playwright MCP browser tools (same setup as `new_autoamtion _fix.py`).

The browser runs on the host through `npx @playwright/mcp`, not in the sandbox:
the sandbox has no network by design. Only the job-search subagent gets these
tools.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

# A real Chrome identity: headless Chromium's default UA is a common block trigger.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def playwright_server_config(
    profile_dir: str | Path, output_dir: str | Path, *, headless: bool = True
) -> dict[str, Any]:
    """MCP server config for `@playwright/mcp` (stdio)."""
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
    return {"command": "npx", "args": args, "transport": "stdio"}


@asynccontextmanager
async def playwright_browser_tools(
    profile_dir: str | Path, output_dir: str | Path, *, headless: bool = True
) -> AsyncIterator[list[BaseTool]]:
    """Yield the Playwright MCP tools bound to ONE long-lived browser session.

    `client.get_tools()` would open a fresh MCP session per tool call, so the
    browser would reset between navigate and evaluate and every page would look
    blank. Holding one session for the whole run keeps the page state.
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_mcp_adapters.tools import load_mcp_tools

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    client = MultiServerMCPClient(
        {"playwright": playwright_server_config(profile_dir, output_dir, headless=headless)}
    )
    async with client.session("playwright") as session:
        yield await load_mcp_tools(session)


__all__ = ["USER_AGENT", "playwright_browser_tools", "playwright_server_config"]
