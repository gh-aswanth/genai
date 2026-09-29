"""Wiring tests for the JobHunter agent. No Docker, network or API calls."""

from __future__ import annotations

import re

import pytest
from deepagents.middleware.memory import MemoryMiddleware
from deepagents.middleware.summarization import SummarizationToolMiddleware
from genai_agentic_sandbox import prompts
from genai_agentic_sandbox.agent import (
    ATS_REVIEWER_SKILLS,
    JOB_MATCHER_SKILLS,
    JOB_SEARCH_SKILLS,
    MAIN_SKILLS,
    RESUME_BUILDER_SKILLS,
    SKILLS_MOUNT,
    SUBAGENT_MEMORY,
    build_subagents,
    create_jobhunter_agent,
    extra_middleware,
)
from genai_agentic_sandbox.caching import OpenAIPromptCachingMiddleware
from genai_agentic_sandbox.main import SKILLS_DIR
from genai_agentic_sandbox.middleware import TodoCompletionMiddleware
from genai_agentic_sandbox.sandbox.docker import DockerSandboxBackend
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI


@tool
def browser_navigate(url: str) -> str:
    """Navigate the browser to a URL."""
    return url


@tool
def browser_evaluate(function: str) -> str:
    """Evaluate JavaScript in the page."""
    return function


BROWSER_TOOLS = [browser_navigate, browser_evaluate]


@pytest.fixture
def model():
    return ChatOpenAI(model="gpt-5.5", api_key="sk-test")


@pytest.fixture
def backend():
    return DockerSandboxBackend()  # lazy: never started by these tests


@pytest.fixture
def subagents(model, backend):
    return {s["name"]: s for s in build_subagents(model, backend, BROWSER_TOOLS)}


def tool_names(tools) -> set[str]:
    return {t.name for t in tools}


# -- middleware --------------------------------------------------------------------------


def test_extra_middleware_adds_only_what_deepagents_lacks(model, backend):
    kinds = [type(m) for m in extra_middleware(model, backend)]
    assert kinds == [
        TodoListMiddleware,
        TodoCompletionMiddleware,
        SummarizationToolMiddleware,
        OpenAIPromptCachingMiddleware,
    ]


def test_extra_middleware_instances_are_not_shared(model, backend):
    a, b = extra_middleware(model, backend), extra_middleware(model, backend)
    assert all(x is not y for x, y in zip(a, b, strict=True))


# -- subagents -----------------------------------------------------------------------------


def test_four_subagents(subagents):
    assert set(subagents) == {"job-search", "job-matcher", "resume-builder", "ats-reviewer"}


def test_job_search_gets_browser_tools_only(subagents):
    assert tool_names(subagents["job-search"]["tools"]) == {"browser_navigate", "browser_evaluate"}
    assert subagents["job-search"]["skills"] == JOB_SEARCH_SKILLS


@pytest.mark.parametrize("name", ["job-matcher", "resume-builder", "ats-reviewer"])
def test_document_agents_have_no_custom_tools(subagents, name):
    """They read and revise documents by writing Python and running it with `execute`."""
    assert subagents[name]["tools"] == []


def test_skills_per_subagent(subagents):
    assert subagents["job-matcher"]["skills"] == JOB_MATCHER_SKILLS
    assert subagents["resume-builder"]["skills"] == RESUME_BUILDER_SKILLS
    assert subagents["ats-reviewer"]["skills"] == ATS_REVIEWER_SKILLS


@pytest.mark.parametrize("name", ["job-search", "job-matcher", "resume-builder", "ats-reviewer"])
def test_every_subagent_plans_and_can_compact(subagents, name):
    kinds = {type(m) for m in subagents[name]["middleware"]}
    expected = {
        TodoListMiddleware,
        TodoCompletionMiddleware,
        SummarizationToolMiddleware,
        OpenAIPromptCachingMiddleware,
    }
    if SUBAGENT_MEMORY[name]:
        expected.add(MemoryMiddleware)
    assert kinds == expected
    cache = next(
        m for m in subagents[name]["middleware"] if isinstance(m, OpenAIPromptCachingMiddleware)
    )
    assert cache.cache_key == f"jobhunter:{name}"
    assert "Plan with `write_todos`" in subagents[name]["system_prompt"]
    assert subagents[name]["system_prompt"]
    assert subagents[name]["description"]


# -- main graph --------------------------------------------------------------------------


def test_main_agent_tools(model, backend):
    graph = create_jobhunter_agent(
        model=model, backend=backend, browser_tools=BROWSER_TOOLS, resume_path="/input/cv.docx"
    )
    tools = graph.nodes["tools"].bound.tools_by_name
    # Built-ins from create_deep_agent (filesystem + sandbox execute + task) ...
    assert {"ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute", "task"} <= set(
        tools
    )
    # ... plus what we added: planning and manual compaction. Nothing else - the
    # agent does document work with code it writes and runs.
    assert {"write_todos", "compact_conversation"} <= set(tools)
    assert not {"read_docx", "apply_docx_revisions", "preview_docx"} & set(tools)
    # The browser belongs to the job-search subagent, not the orchestrator.
    assert "browser_navigate" not in tools
    task = tools["task"].description
    for name in ("job-search", "job-matcher", "resume-builder", "ats-reviewer"):
        assert f"- {name}:" in task


def test_main_graph_runs_skills_and_todo_middleware(model, backend):
    graph = create_jobhunter_agent(
        model=model, backend=backend, browser_tools=[], resume_path="/input/cv.docx"
    )
    assert "SkillsMiddleware.before_agent" in graph.nodes
    assert "TodoListMiddleware.after_model" in graph.nodes
    assert "TodoCompletionMiddleware.after_model" in graph.nodes


def test_resume_path_is_in_the_main_prompt():
    assert "/input/my cv.docx" in prompts.jobhunter_prompt("/input/my cv.docx")


# -- skills --------------------------------------------------------------------------------


def _frontmatter(path) -> dict:
    text = path.read_text()
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, f"{path} has no frontmatter"
    return dict(line.split(": ", 1) for line in match.group(1).splitlines())


@pytest.mark.parametrize(
    "sources",
    [
        MAIN_SKILLS,
        JOB_SEARCH_SKILLS,
        JOB_MATCHER_SKILLS,
        RESUME_BUILDER_SKILLS,
        ATS_REVIEWER_SKILLS,
    ],
)
def test_skill_sources_exist_and_are_valid(sources):
    for source in sources:
        assert source.startswith(SKILLS_MOUNT + "/")
        host_dir = SKILLS_DIR / source[len(SKILLS_MOUNT) + 1 :]
        skills = sorted(host_dir.glob("*/SKILL.md"))
        assert skills, f"no skills in {host_dir}"
        for skill in skills:
            meta = _frontmatter(skill)
            assert meta["name"] == skill.parent.name
            assert re.fullmatch(r"[a-z0-9-]{1,64}", meta["name"])
            assert 20 < len(meta["description"]) <= 1024


def test_skill_python_blocks_compile():
    """Every top-level python snippet the skills teach is at least valid Python."""
    for skill in SKILLS_DIR.glob("*/*/SKILL.md"):
        for block in re.findall(
            r"^```python\n(.*?)\n```$", skill.read_text(), re.DOTALL | re.MULTILINE
        ):
            compile(block, str(skill), "exec")


@pytest.mark.parametrize(
    "prompt",
    ["JOB_SEARCH_PROMPT", "JOB_MATCHER_PROMPT", "RESUME_BUILDER_PROMPT", "ATS_REVIEWER_PROMPT"],
)
def test_subagent_prompts_are_code_first(prompt):
    text = getattr(prompts, prompt)
    assert "`execute`" in text and "write_file" in text


def test_file_contract_is_consistent():
    orch = SKILLS_DIR / "orchestrator"
    workflow = (orch / "jobhunt-workflow" / "SKILL.md").read_text()
    loop = (orch / "resume-optimization" / "SKILL.md").read_text()
    resume_skill = (SKILLS_DIR / "resume" / "docx-tracked-revisions" / "SKILL.md").read_text()
    matcher_skill = (SKILLS_DIR / "matching" / "ats-resume-review" / "SKILL.md").read_text()
    assert prompts.JOBS_FILE in workflow and prompts.MATCH_REPORT in workflow
    for path in (prompts.TAILORED_DIR, prompts.ROUND_DIR, prompts.ATS_ROUND_FILE):
        assert path in loop, path
    assert prompts.REVISION_SCRIPT in resume_skill
    assert prompts.REVISION_SCRIPT in prompts.RESUME_BUILDER_PROMPT
    assert prompts.JOBS_FILE in prompts.JOB_SEARCH_PROMPT
    assert prompts.JOBS_FILE in prompts.JOB_MATCHER_PROMPT
    assert prompts.MATCH_REPORT in prompts.RESUME_BUILDER_PROMPT
    assert '"selected_jobs"' in matcher_skill
    assert "selected_jobs" in prompts.RESUME_BUILDER_PROMPT


def test_top_jobs_reaches_the_orchestrator(model, backend):
    prompt = prompts.jobhunter_prompt("/input/cv.docx", top_jobs=3)
    assert "jobs to select and tailor: 3" in prompt
    assert "Always read `agent-orchestration` first" in prompt
    graph = create_jobhunter_agent(
        model=model, backend=backend, browser_tools=[], resume_path="/input/cv.docx", top_jobs=3
    )
    assert graph is not None


def test_resume_builder_is_one_job_and_round_per_task(subagents):
    builder = subagents["resume-builder"]
    assert "ONE selected job and ONE round" in builder["description"]
    assert "Each task is ONE selected job and ONE round" in builder["system_prompt"]
    for out in ("_redline.docx", "_review.docx", "_final.docx"):
        assert out in builder["system_prompt"]


def test_ats_reviewer_scores_only_the_final_copy(subagents):
    reviewer = subagents["ats-reviewer"]["system_prompt"]
    assert "In loop mode score only the *_final.docx" in reviewer
    assert "You\nnever edit a resume" in reviewer or "never edit a resume" in reviewer.replace(
        "\n", " "
    )
    assert '"score-only"' in reviewer
    assert "UNCHANGED so the scores are comparable" in reviewer
    assert prompts.ATS_ROUND_FILE in reviewer


def test_run_settings_reach_the_orchestrator():
    prompt = prompts.jobhunter_prompt("/input/cv.docx", top_jobs=2, target_score=90, max_rounds=4)
    assert "ATS target score: 90; max rounds per job: 4" in prompt
    assert f"gains less than {prompts.MIN_GAIN} points" in prompt
    assert "run the `output-cleanup` skill as the last step" in prompt
    keep = prompts.jobhunter_prompt("/input/cv.docx", cleanup=False)
    assert "do NOT run `output-cleanup`" in keep
    loop = (SKILLS_DIR / "orchestrator" / "resume-optimization" / "SKILL.md").read_text()
    assert "ats/round-<k>.json" in loop and "best_round" in loop


def test_orchestrator_plans_with_todos():
    prompt = prompts.jobhunter_prompt("/input/cv.docx")
    assert prompts.TODO_DISCIPLINE in prompt
    routing = (SKILLS_DIR / "orchestrator" / "agent-orchestration" / "SKILL.md").read_text()
    assert "Call `write_todos` BEFORE the first delegation" in routing


def test_main_agent_skills_are_the_orchestrator_folder():
    assert MAIN_SKILLS == ["/skills/orchestrator/"]


@pytest.mark.parametrize("prompt", ["JOB_MATCHER_PROMPT", "RESUME_BUILDER_PROMPT"])
def test_empty_resume_stops_instead_of_inventing(prompt):
    text = getattr(prompts, prompt)
    assert "STOP" in text and "Never build a skeleton" in text
