"""JobHunter: a Deep Agent with subagents, running on a Docker sandbox.

    jobhunter (main, orchestrator)
    ├── job-search       Playwright MCP browser tools -> /output/jobs/jobs.json
    ├── job-matcher      resume + jobs -> ATS review, selected jobs, change lists
    ├── job-optimizer    ONE PER SELECTED JOB, all launched in one message and
    │   │                running in parallel; each owns its job's whole loop:
    │   ├── resume-builder   round k: colour-coded redline, tracked review, clean final
    │   └── ats-reviewer     round k: fixed ATS score + feedback -> next round or done
    └── ats-reviewer     score-only workflow (the user's resume as it is)

Middleware
----------
`create_deep_agent` already provides, for the main agent and every subagent:

- `FilesystemMiddleware`  (ls/read_file/write_file/edit_file/glob/grep, plus
  `execute` because the backend is a sandbox)
- `SubAgentMiddleware`    (the `task` tool; main agent)
- `SummarizationMiddleware` (automatic compaction)
- `SkillsMiddleware`      (when `skills=` is given)

so those are configured, not added again. Added explicitly to the main agent and
every subagent:

- `TodoListMiddleware`    (`write_todos` planning; not in the default stack)
- `TodoCompletionMiddleware` (ours: the work tools of each agent are refused
  until it has planned with `write_todos` in this request, and it is sent back
  every time it tries to finish while a todo is still open)
- `create_summarization_tool_middleware` (`compact_conversation` tool; shares
  state with the automatic summarization)

Planning is the todo list - the single source of truth, no plan files (skill
`workflow-planning`). See `PLAN_BEFORE` for which tools need a plan first.

Skills: the orchestrator (main agent) has the workflow skills under
/skills/orchestrator/ (agent-orchestration, jobhunt-workflow,
resume-optimization, ats-score-only, job-posting-intake, output-cleanup); each
subagent has only the skills of its own job.

The backend is `DockerSandboxBackend`: every file operation and command runs in
the container, which sees only the mounts (`/input` resume, `/output`,
`/skills`). There are no special-purpose file tools: agents write Python at run
time (python-docx, lxml, pandas are in the image) and run it with `execute`;
results land in the `/output` mount. The only non-sandbox tools are the
Playwright MCP browser tools, given to job-search alone, because the sandbox
has no network by design.

Memory: `user_profile.md` and `agent_notes.md` in the host memory folder
(mounted at /memories) are loaded into the orchestrator (read-write, via
`create_deep_agent(memory=...)`) and, read-only, into the subagents that need
them - see `memory.py`. The ats-reviewer gets none, to stay independent.

Prompt caching: OpenAI, not Anthropic - see `caching.py`. Deep Agents'
Anthropic caching middleware is removed through the `openai` harness profile,
and every agent routes its calls to its own OpenAI prompt cache.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from deepagents import CompiledSubAgent, SubAgent, create_deep_agent
from deepagents.backends.protocol import SandboxBackendProtocol
from deepagents.middleware.summarization import create_summarization_tool_middleware
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from genai_agentic_sandbox.caching import (
    CacheRetention,
    OpenAIPromptCachingMiddleware,
    disable_anthropic_prompt_caching,
)
from genai_agentic_sandbox.memory import (
    AGENT_NOTES,
    USER_PROFILE,
    MemoryGuardMiddleware,
    subagent_memory,
)
from genai_agentic_sandbox.middleware import TodoCompletionMiddleware
from genai_agentic_sandbox.prompts import (
    ATS_REVIEWER_PROMPT,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_TARGET_SCORE,
    JOB_MATCHER_PROMPT,
    JOB_SEARCH_PROMPT,
    RESUME_BUILDER_PROMPT,
    job_optimizer_prompt,
    jobhunter_prompt,
)
from genai_agentic_sandbox.tools.browser import BrowserFileGuardMiddleware

SKILLS_MOUNT = "/skills"
MAIN_SKILLS = [f"{SKILLS_MOUNT}/orchestrator/"]
JOB_SEARCH_SKILLS = [f"{SKILLS_MOUNT}/search/"]
JOB_MATCHER_SKILLS = [f"{SKILLS_MOUNT}/matching/"]
RESUME_BUILDER_SKILLS = [f"{SKILLS_MOUNT}/resume/"]
ATS_REVIEWER_SKILLS = [f"{SKILLS_MOUNT}/ats/"]
JOB_OPTIMIZER_SKILLS = [f"{SKILLS_MOUNT}/optimizer/"]

# Long-term memory each agent loads (the reviewer none: independent scores).
MAIN_MEMORY = [USER_PROFILE, AGENT_NOTES]
SUBAGENT_MEMORY = {
    "job-search": [USER_PROFILE, AGENT_NOTES],
    "job-matcher": [USER_PROFILE],
    "resume-builder": [USER_PROFILE],
    "ats-reviewer": [],
    "job-optimizer": [],
}


# Tools that do the real work: refused until the agent planned with write_todos.
PLAN_BEFORE = {
    "orchestrator": ("task",),
    "job-search": ("execute", "write_file", "edit_file", "browser_navigate"),
    "job-matcher": ("execute", "write_file", "edit_file"),
    "resume-builder": ("execute", "write_file", "edit_file"),
    "ats-reviewer": ("execute", "write_file", "edit_file"),
    "job-optimizer": ("task", "execute", "write_file", "edit_file"),
}


def extra_middleware(
    model: BaseChatModel,
    backend: SandboxBackendProtocol,
    agent: str = "orchestrator",
    *,
    cache_retention: CacheRetention | None = None,
    memory: list[str] | None = None,
) -> list[AgentMiddleware]:
    """Middleware the deep agent stack does not already include (fresh instances).

    Args:
        agent: Agent name, used for its OpenAI prompt-cache key.
        cache_retention: OpenAI `prompt_cache_retention` ("in_memory" / "24h").
        memory: Memory files to load read-only (subagents). The orchestrator's
            memory is passed to `create_deep_agent(memory=...)` instead.
    """
    stack: list[AgentMiddleware] = [
        TodoListMiddleware(),
        # Plan with todos before working; finish only when every todo is completed.
        TodoCompletionMiddleware(plan_before=PLAN_BEFORE.get(agent, ())),
        create_summarization_tool_middleware(model, backend),
        OpenAIPromptCachingMiddleware(agent, retention=cache_retention),
        # Memory is never deleted; subagents may not write it at all.
        MemoryGuardMiddleware(read_only=agent != "orchestrator"),
    ]
    if memory:
        stack.append(subagent_memory(backend, memory))
    return stack


def _middleware_factory(
    model: BaseChatModel,
    backend: SandboxBackendProtocol,
    cache_retention: CacheRetention | None,
    memory: bool,
) -> Callable[[str], list[AgentMiddleware]]:
    def mw(agent: str) -> list[AgentMiddleware]:
        sources = SUBAGENT_MEMORY.get(agent) if memory else None
        return extra_middleware(
            model, backend, agent, cache_retention=cache_retention, memory=sources
        )

    return mw


def _resume_builder_spec(mw: Callable[[str], list[AgentMiddleware]]) -> SubAgent:
    return {
        "name": "resume-builder",
        "description": (
            "Builds ONE tailored resume for ONE selected job and ONE round: from the "
            "original resume, applies that job's changes (and, from round 2, the ATS "
            "feedback) as colour-coded tracked changes with comments, writing redline, "
            "review and final copies to /output/resume/<rank>-<slug>/v<k>/. Give it the "
            "resume path, job id/rank/slug, round k, folder and feedback file."
        ),
        "system_prompt": RESUME_BUILDER_PROMPT,
        "tools": [],
        "skills": RESUME_BUILDER_SKILLS,
        "middleware": mw("resume-builder"),
    }


def _ats_reviewer_spec(mw: Callable[[str], list[AgentMiddleware]]) -> SubAgent:
    return {
        "name": "ats-reviewer",
        "description": (
            "Independently scores ONE resume for ONE job like an ATS - fixed reproducible "
            "scoring script plus a quality review. Loop mode: a round's *_final.docx -> "
            "/output/resume/<rank>-<slug>/ats/round-<k>.json (score, issues, verdict). "
            "Score-only mode: the user's resume as is -> /output/ats/<slug>/round-1.json. "
            "Give it the mode, job id/rank/slug, round k, target score and the .docx path."
        ),
        "system_prompt": ATS_REVIEWER_PROMPT,
        "tools": [],
        "skills": ATS_REVIEWER_SKILLS,
        "middleware": mw("ats-reviewer"),
    }


def optimizer_subagents(
    model: BaseChatModel,
    backend: SandboxBackendProtocol,
    *,
    cache_retention: CacheRetention | None = None,
    memory: bool = True,
) -> list[SubAgent]:
    """The job-optimizer's own subagents: resume-builder and ats-reviewer."""
    mw = _middleware_factory(model, backend, cache_retention, memory)
    return [_resume_builder_spec(mw), _ats_reviewer_spec(mw)]


def build_job_optimizer(
    model: BaseChatModel,
    backend: SandboxBackendProtocol,
    *,
    cache_retention: CacheRetention | None = None,
    memory: bool = True,
    target_score: float = DEFAULT_TARGET_SCORE,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> CompiledSubAgent:
    """One job's whole builder <-> reviewer loop, as a subagent of its own.

    The orchestrator launches one per selected job in a single message, so every
    job's loop runs concurrently instead of the orchestrator stepping the jobs
    round by round.
    """
    runnable = create_deep_agent(
        model=model,
        tools=[],
        system_prompt=job_optimizer_prompt(target_score, max_rounds),
        subagents=optimizer_subagents(
            model, backend, cache_retention=cache_retention, memory=memory
        ),
        skills=JOB_OPTIMIZER_SKILLS,
        backend=backend,
        middleware=extra_middleware(
            model, backend, "job-optimizer", cache_retention=cache_retention
        ),
        name="job-optimizer",
    )
    return {
        "name": "job-optimizer",
        "description": (
            "Runs the WHOLE resume-optimisation loop for ONE selected job on its own: "
            "resume-builder round -> ats-reviewer score -> repeat until the target score, "
            "no more gain, or the round limit -> copy the best round's redline / review / "
            "final .docx into /output/resume/<rank>-<slug>/ and write ats_history.json. "
            "Launch one per selected job, ALL in the same message, so the jobs run in "
            "parallel. Give it the resume path, the job id / rank / title / company / slug "
            "and its folder."
        ),
        "runnable": runnable,
    }


def build_subagents(
    model: BaseChatModel,
    backend: SandboxBackendProtocol,
    browser_tools: Sequence[BaseTool],
    *,
    cache_retention: CacheRetention | None = None,
    memory: bool = True,
    target_score: float = DEFAULT_TARGET_SCORE,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> list[SubAgent | CompiledSubAgent]:
    """The orchestrator's subagents: job-search, job-matcher, job-optimizer, ats-reviewer.

    Each gets the deep-agent built-ins (filesystem tools + `execute` in the
    sandbox). `tools` is set explicitly so nothing is inherited implicitly:
    job-search adds the browser, the others need nothing beyond code execution.
    job-optimizer is a deep agent itself (resume-builder + ats-reviewer); the
    orchestrator keeps ats-reviewer for the score-only workflow.
    """
    mw = _middleware_factory(model, backend, cache_retention, memory)
    job_search: SubAgent = {
        "name": "job-search",
        "description": (
            "Finds current job postings on the web with a real browser (Playwright). "
            "Give it the role, location, seniority, sites/URLs, count and date limits. "
            "Saves normalised jobs to /output/jobs/jobs.json."
        ),
        "system_prompt": JOB_SEARCH_PROMPT,
        "tools": list(browser_tools),
        "skills": JOB_SEARCH_SKILLS,
        # browser files stay in the captures folder, never elsewhere on the host
        "middleware": [*mw("job-search"), BrowserFileGuardMiddleware()],
    }
    job_matcher: SubAgent = {
        "name": "job-matcher",
        "description": (
            "Matches the resume against /output/jobs/jobs.json: scores each job, does an "
            "ATS review, selects the best N jobs and writes a separate change list for "
            "each (required skills, keywords, rewrites). Writes "
            "/output/match/match_report.json and .md. Give it the resume path and N."
        ),
        "system_prompt": JOB_MATCHER_PROMPT,
        "tools": [],
        "skills": JOB_MATCHER_SKILLS,
        "middleware": mw("job-matcher"),
    }
    return [
        job_search,
        job_matcher,
        build_job_optimizer(
            model,
            backend,
            cache_retention=cache_retention,
            memory=memory,
            target_score=target_score,
            max_rounds=max_rounds,
        ),
        _ats_reviewer_spec(mw),
    ]


def declarative_specs(
    model: BaseChatModel,
    backend: SandboxBackendProtocol,
    browser_tools: Sequence[BaseTool] = (),
    *,
    memory: bool = True,
) -> dict[str, SubAgent]:
    """Every declarative agent spec at every level, by name: the orchestrator's
    (job-search, job-matcher, ats-reviewer) and the job-optimizer's resume-builder.
    (The job-optimizer itself is compiled; it is not in this map.)"""
    top = [
        s
        for s in build_subagents(model, backend, browser_tools, memory=memory)
        if "runnable" not in s
    ]
    nested = optimizer_subagents(model, backend, memory=memory)
    return {**{s["name"]: s for s in nested}, **{s["name"]: s for s in top}}


def create_jobhunter_agent(
    *,
    model: BaseChatModel,
    backend: SandboxBackendProtocol,
    browser_tools: Sequence[BaseTool],
    resume_path: str,
    top_jobs: int = 2,
    target_score: float = DEFAULT_TARGET_SCORE,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    cleanup: bool = True,
    memory: bool = True,
    cache_retention: CacheRetention | None = None,
    checkpointer: Any = None,
    name: str = "jobhunter",
):
    """Create the JobHunter deep agent.

    Args:
        model: Chat model shared by the main agent and subagents.
        backend: A sandbox backend (normally `DockerSandboxBackend`) with the
            resume mounted at `resume_path`, a writable `/output` and the
            package skills at `/skills`.
        browser_tools: Playwright MCP tools (see `tools.browser`), given only
            to the job-search subagent.
        resume_path: Sandbox path of the (read-only copy of the) .docx resume,
            e.g. `/input/resume.docx`.
        top_jobs: How many best-matching jobs get their own tailored resume.
        target_score: ATS score at which a job's improvement loop stops.
        max_rounds: Maximum builder <-> reviewer rounds per job.
        cleanup: Remove working files at the end, keeping only .docx deliverables.
        memory: Load long-term memory from /memories (the backend must mount it).
        cache_retention: OpenAI prompt cache retention ("in_memory" / "24h").
        checkpointer: Optional LangGraph checkpointer for multi-turn sessions.
        name: Graph name.
    """
    disable_anthropic_prompt_caching()
    return create_deep_agent(
        model=model,
        tools=[],
        system_prompt=jobhunter_prompt(
            resume_path, top_jobs, target_score, max_rounds, cleanup, memory=memory
        ),
        subagents=build_subagents(
            model,
            backend,
            browser_tools,
            cache_retention=cache_retention,
            memory=memory,
            target_score=target_score,
            max_rounds=max_rounds,
        ),
        skills=MAIN_SKILLS,
        memory=MAIN_MEMORY if memory else None,
        backend=backend,
        middleware=extra_middleware(
            model, backend, "orchestrator", cache_retention=cache_retention
        ),
        checkpointer=checkpointer,
        name=name,
    )


__all__ = [
    "JOB_MATCHER_SKILLS",
    "JOB_SEARCH_SKILLS",
    "MAIN_MEMORY",
    "MAIN_SKILLS",
    "PLAN_BEFORE",
    "RESUME_BUILDER_SKILLS",
    "SKILLS_MOUNT",
    "SUBAGENT_MEMORY",
    "build_job_optimizer",
    "build_subagents",
    "create_jobhunter_agent",
    "declarative_specs",
    "extra_middleware",
    "optimizer_subagents",
]
