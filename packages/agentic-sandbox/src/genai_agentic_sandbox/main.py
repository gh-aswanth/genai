"""`genai-agentic-sandbox`: run the JobHunter agent.

    uv run genai-agentic-sandbox --resume ~/cv.docx \
        "find senior python backend jobs in Kochi, match my resume and revise it"

Without a task it starts an interactive session (/new, /quit).

Your resume is never touched: it is checked (it must contain text), copied to
<out>/original/, and only that copy is mounted, read-only. Each tailored resume
is a new file under <out>/resume/<rank>-<company>/.

What runs where:
- The agent's filesystem and `execute` run in a Docker sandbox built from
  `builder/sandbox.pyproject.toml` (no network). It sees only:
  /input/<resume>.docx and /output/original (read-only copy of your resume),
  /output (read-write -> --out on the host), /skills (read-only).
- The browser (Playwright MCP) runs on the host, used by the job-search subagent.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import shutil
import stat
import sys
import uuid
import zipfile
from pathlib import Path

from genai_agentic_sandbox.agent import SKILLS_MOUNT, create_jobhunter_agent
from genai_agentic_sandbox.caching import cache_stats
from genai_agentic_sandbox.memory import (
    MEMORY_DIRNAME,
    MEMORY_MOUNT,
    describe_memory,
    seed_memory,
)
from genai_agentic_sandbox.prompts import DEFAULT_MAX_ROUNDS, DEFAULT_TARGET_SCORE
from genai_agentic_sandbox.sandbox.docker import Mount

PACKAGE_DIR = Path(__file__).resolve().parent
SKILLS_DIR = PACKAGE_DIR / "skills"
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_TOP_JOBS = 2
# A real resume has hundreds of characters; fewer means an empty/blank document.
MIN_RESUME_CHARS = 200


class ResumeError(ValueError):
    """The resume file cannot be used (missing, not a .docx, or has no text)."""


def docx_text_chars(path: Path) -> int:
    """Characters of visible text in a .docx body (no python-docx needed)."""
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ResumeError(f"{path} is not a readable .docx: {exc}") from exc
    return sum(len(m) for m in re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", xml))


def prepare_resume(resume: Path, out: Path) -> Path:
    """Validate the resume and copy it to <out>/original/ (read-only). Returns the copy.

    The agent only ever sees this copy. The user's file is not mounted.
    """
    resume = resume.expanduser().resolve()
    if resume.suffix.lower() != ".docx" or not resume.is_file():
        raise ResumeError(f"{resume} is not a .docx file")
    chars = docx_text_chars(resume)
    if chars < MIN_RESUME_CHARS:
        raise ResumeError(
            f"{resume} contains only {chars} characters of text - it looks empty. "
            "Pass the .docx that holds your resume (open it in Word to check)."
        )
    original_dir = out / "original"
    original_dir.mkdir(parents=True, exist_ok=True)
    copy = original_dir / resume.name
    if copy.exists():
        copy.chmod(stat.S_IRUSR | stat.S_IWUSR)
    shutil.copy2(resume, copy)
    copy.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # read-only on the host too
    if hashlib.sha256(copy.read_bytes()).digest() != hashlib.sha256(resume.read_bytes()).digest():
        raise ResumeError(f"copying {resume} to {copy} produced a different file")
    return copy


def sandbox_mounts(resume_copy: Path, out: Path, memory_dir: Path | None = None) -> list[Mount]:
    """/output read-write, but the resume copy read-only wherever it appears.

    `memory_dir` (long-term memory, outside the output folder) is mounted
    read-write at /memories when given.
    """
    original_dir = resume_copy.parent
    memory = [Mount(memory_dir, MEMORY_MOUNT, read_only=False)] if memory_dir else []
    return memory + [
        Mount(out, "/output", read_only=False),
        # Nested read-only mount over /output/original: the rw /output mount cannot
        # be used to overwrite the copy either.
        Mount(original_dir, "/output/original", read_only=True),
        Mount(original_dir, "/input", read_only=True),
        Mount(SKILLS_DIR, SKILLS_MOUNT, read_only=True),
    ]


BANNER = """
JobHunter - ask for jobs, a resume match, or a revised resume.

  find python backend jobs in Kochi posted this month, match my resume and revise it
  just find 10 data engineer jobs on infopark.in
  which required skills am I missing most often?

Commands:  /new  fresh session    /quit  exit
"""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="JobHunter deep agent on a Docker sandbox.")
    parser.add_argument("task", nargs="?", help="what to do; omit for an interactive session")
    parser.add_argument("--resume", type=Path, required=True, help="your resume (.docx)")
    parser.add_argument("--out", type=Path, default=Path("jobhunt-output"), help="output folder")
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--top-jobs",
        type=int,
        default=DEFAULT_TOP_JOBS,
        help="how many best-matching jobs get their own tailored resume (default 2)",
    )
    parser.add_argument(
        "--target-score",
        type=float,
        default=DEFAULT_TARGET_SCORE,
        help="ATS score at which a job's improvement loop stops (default 85)",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=DEFAULT_MAX_ROUNDS,
        help="maximum resume-builder <-> ATS-reviewer rounds per job (default 3)",
    )
    parser.add_argument(
        "--keep-work-files",
        action="store_true",
        help="keep the agents' working files (scripts, JSON, rounds); default: only .docx outputs",
    )
    parser.add_argument(
        "--memory-dir",
        type=Path,
        default=None,
        help="long-term memory folder kept between runs (default: <out>/memories)",
    )
    parser.add_argument("--no-memory", action="store_true", help="run without long-term memory")
    parser.add_argument(
        "--prompt-cache-retention",
        choices=["in_memory", "24h"],
        default=None,
        help="OpenAI prompt cache retention (24h needs a model that supports it)",
    )
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--no-verify", action="store_true", help="skip sandbox image check")
    return parser.parse_args(argv)


_TODO_MARK = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}


def format_todos(who: str, todos: list[dict]) -> str:
    """The plan (todo list) as the terminal shows it whenever an agent updates it."""
    done = sum(t.get("status") == "completed" for t in todos)
    lines = [f"  [{who}] plan - {done}/{len(todos)} done"]
    lines += [
        f"      {_TODO_MARK.get(t.get('status'), '[ ]')} {t.get('content', '')}" for t in todos
    ]
    return "\n".join(lines)


def _print_stream_chunk(namespace: tuple, update: dict) -> None:
    """Print tool calls (main and subagents) and the main agent's replies."""
    who = namespace[-1].split(":")[0] if namespace else "jobhunter"
    for data in update.values():
        if not isinstance(data, dict):
            continue
        messages = data.get("messages")
        if not isinstance(messages, list):
            continue
        for message in messages:
            if getattr(message, "type", None) != "ai":
                continue
            for call in getattr(message, "tool_calls", None) or []:
                args = call["args"]
                if call["name"] == "write_todos":  # the plan: show it in full
                    print(format_todos(who, args.get("todos") or []))
                    continue
                if call["name"] == "task":
                    args = {
                        "subagent": args.get("subagent_type"),
                        "task": str(args.get("description"))[:200],
                    }
                print(f"  [{who}] -> {call['name']}({str(args)[:160]})")
            if message.content and not namespace and not getattr(message, "tool_calls", None):
                text = message.content if isinstance(message.content, str) else message.text
                print(f"\njobhunter > {text}\n")


def _ai_messages(update: dict) -> list:
    out = []
    for data in update.values():
        messages = data.get("messages") if isinstance(data, dict) else None
        if isinstance(messages, list):
            out += [m for m in messages if getattr(m, "type", None) == "ai"]
    return out


async def _run_turn(agent, text: str, thread_id: str) -> None:
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 1000}
    seen = []
    async for namespace, update in agent.astream(
        {"messages": [{"role": "user", "content": text}]},
        config=config,
        stream_mode="updates",
        subgraphs=True,
    ):
        _print_stream_chunk(namespace, update)
        seen += _ai_messages(update)
    stats = cache_stats(seen)
    if stats["input_tokens"]:
        share = 100 * stats["cached_tokens"] / stats["input_tokens"]
        print(
            f"[prompt cache] {stats['cached_tokens']:,} of {stats['input_tokens']:,} input "
            f"tokens served from OpenAI's cache ({share:.0f}%) over {stats['calls']} calls"
        )


async def _chat(agent, first: str | None) -> None:
    thread = f"session-{uuid.uuid4().hex[:8]}"
    if first:
        await _run_turn(agent, first, thread)
        return
    print(BANNER)
    while True:
        try:
            text = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return
        if not text:
            continue
        if text.lower() in {"/quit", "/exit", "quit", "exit"}:
            print("bye")
            return
        if text.lower() == "/new":
            thread = f"session-{uuid.uuid4().hex[:8]}"
            print("New session. Files in the output folder are kept.\n")
            continue
        try:
            await _run_turn(agent, text, thread)
        except KeyboardInterrupt:
            print("\n[stopped]\n")
        except Exception as exc:  # noqa: BLE001 - keep the session alive
            print(f"\n[error: {type(exc).__name__}: {exc}]\n")


async def run(args: argparse.Namespace) -> int:
    from langchain.chat_models import init_chat_model
    from langgraph.checkpoint.memory import MemorySaver

    from genai_agentic_sandbox.builder import SandboxImageBuilder, SandboxImageBuildError
    from genai_agentic_sandbox.tools.browser import playwright_browser_tools

    out = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    try:
        resume_copy = prepare_resume(args.resume, out)
    except ResumeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    resume_in_sandbox = f"/input/{resume_copy.name}"
    if args.top_jobs < 1 or args.max_rounds < 1 or not 0 < args.target_score <= 100:
        print(
            "error: need --top-jobs >= 1, --max-rounds >= 1, 0 < --target-score <= 100",
            file=sys.stderr,
        )
        return 2

    memory_dir = None if args.no_memory else seed_memory(args.memory_dir or out / MEMORY_DIRNAME)
    model = init_chat_model(f"openai:{args.model}", temperature=0)
    try:
        backend = SandboxImageBuilder().backend(
            verify=not args.no_verify, mounts=sandbox_mounts(resume_copy, out, memory_dir)
        )
    except SandboxImageBuildError as exc:
        print(f"error: sandbox image: {exc}", file=sys.stderr)
        return 1

    with backend:
        async with playwright_browser_tools(
            profile_dir=out / ".browser" / "profile",
            output_dir=out / ".browser" / "captures",
            headless=not args.headed,
        ) as browser_tools:
            print(f"Sandbox {backend.id} ({backend.image}); {len(browser_tools)} browser tools.")
            print(f"Resume copied to {resume_copy} (read-only) -> {resume_in_sandbox}")
            print(describe_memory(memory_dir) if memory_dir else "Memory: off")
            print(
                f"Tailored resumes for the top {args.top_jobs} jobs -> {out / 'resume'} "
                f"(ATS target {args.target_score}, up to {args.max_rounds} rounds each)"
            )
            agent = create_jobhunter_agent(
                model=model,
                backend=backend,
                browser_tools=browser_tools,
                resume_path=resume_in_sandbox,
                top_jobs=args.top_jobs,
                target_score=args.target_score,
                max_rounds=args.max_rounds,
                cleanup=not args.keep_work_files,
                memory=memory_dir is not None,
                cache_retention=args.prompt_cache_retention,
                checkpointer=MemorySaver(),
            )
            await _chat(agent, args.task)
    if memory_dir:
        print(describe_memory(memory_dir))
    return 0


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    args = _parse_args(argv)
    if not os.environ.get("OPENAI_API_KEY"):
        print("error: set OPENAI_API_KEY (e.g. in .env)", file=sys.stderr)
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
