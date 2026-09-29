"""JobHunter: a Deep Agent with three subagents, running on a Docker sandbox.

    jobhunter (main)
    ├── job-search      Playwright MCP browser tools -> /output/jobs/jobs.json
    ├── job-matcher     resume + jobs -> ATS review, required skills, change list
    ├── resume-builder  per selected job and round: colour-coded redline, tracked
    │                   review and clean final copy of the resume
    └── ats-reviewer    per selected job and round: fixed ATS score + feedback;
                        the orchestrator loops builder <-> reviewer until done

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

from collections.abc import Sequence
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends.protocol import SandboxBackendProtocol
from deepagents.middleware.subagents import SubAgent
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
    jobhunter_prompt,
)
from genai_agentic_sandbox.tools.browser import BrowserFileGuardMiddleware

SKILLS_MOUNT = "/skills"
MAIN_SKILLS = [f"{SKILLS_MOUNT}/orchestrator/"]
JOB_SEARCH_SKILLS = [f"{SKILLS_MOUNT}/search/"]
JOB_MATCHER_SKILLS = [f"{SKILLS_MOUNT}/matching/"]
RESUME_BUILDER_SKILLS = [f"{SKILLS_MOUNT}/resume/"]
ATS_REVIEWER_SKILLS = [f"{SKILLS_MOUNT}/ats/"]

# Long-term memory each agent loads (the reviewer none: independent scores).
MAIN_MEMORY = [USER_PROFILE, AGENT_NOTES]
SUBAGENT_MEMORY = {
    "job-search": [USER_PROFILE, AGENT_NOTES],
    "job-matcher": [USER_PROFILE],
    "resume-builder": [USER_PROFILE],
    "ats-reviewer": [],
}


# Tools that do the real work: refused until the agent planned with write_todos.
PLAN_BEFORE = {
    "orchestrator": ("task",),
    "job-search": ("execute", "write_file", "edit_file", "browser_navigate"),
    "job-matcher": ("execute", "write_file", "edit_file"),
    "resume-builder": ("execute", "write_file", "edit_file"),
    "ats-reviewer": ("execute", "write_file", "edit_file"),
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


def build_subagents(
    model: BaseChatModel,
    backend: SandboxBackendProtocol,
    browser_tools: Sequence[BaseTool],
    *,
    cache_retention: CacheRetention | None = None,
    memory: bool = True,
) -> list[SubAgent]:
    """The three specialist subagents.

    Each gets the deep-agent built-ins (filesystem tools + `execute` in the
    sandbox). `tools` is set explicitly so nothing is inherited implicitly:
    job-search adds the browser, the others need nothing beyond code execution.
    """

    def mw(agent: str) -> list[AgentMiddleware]:
        sources = SUBAGENT_MEMORY[agent] if memory else None
        return extra_middleware(
            model, backend, agent, cache_retention=cache_retention, memory=sources
        )

    return [
        {
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
        },
        {
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
        },
        {
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
        },
        {
            "name": "ats-reviewer",
            "description": (
                "Independently scores ONE round's tailored resume (the *_final.docx) for ONE "
                "job like an ATS - fixed reproducible scoring script plus a quality review - "
                "and writes /output/resume/<rank>-<slug>/ats/round-<k>.json with score, "
                "actionable issues and verdict (done/improve). Give it the job id/rank/slug, "
                "round k, target score and the final .docx path."
            ),
            "system_prompt": ATS_REVIEWER_PROMPT,
            "tools": [],
            "skills": ATS_REVIEWER_SKILLS,
            "middleware": mw("ats-reviewer"),
        },
    ]


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
            model, backend, browser_tools, cache_retention=cache_retention, memory=memory
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
    "build_subagents",
    "create_jobhunter_agent",
    "extra_middleware",
]
