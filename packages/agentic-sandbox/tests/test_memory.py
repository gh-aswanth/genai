"""Long-term memory: seeding, mounting, and which agent loads which file."""

from __future__ import annotations

import pytest
from deepagents.middleware.memory import MemoryMiddleware
from genai_agentic_sandbox import prompts
from genai_agentic_sandbox.agent import (
    MAIN_MEMORY,
    SUBAGENT_MEMORY,
    build_subagents,
    create_jobhunter_agent,
)
from genai_agentic_sandbox.main import SKILLS_DIR, prepare_resume, sandbox_mounts
from genai_agentic_sandbox.memory import (
    AGENT_NOTES,
    MEMORY_MOUNT,
    SUBAGENT_MEMORY_PROMPT,
    TEMPLATES,
    USER_PROFILE,
    seed_memory,
)
from genai_agentic_sandbox.sandbox.docker import DockerSandboxBackend
from langchain_openai import ChatOpenAI

from .docx_samples import RESUME


@pytest.fixture
def model():
    return ChatOpenAI(model="gpt-5.5", api_key="sk-test")


def test_seed_creates_templates(tmp_path):
    d = seed_memory(tmp_path / "mem")
    assert sorted(p.name for p in d.iterdir()) == ["agent_notes.md", "user_profile.md"]
    assert "## Confirmed facts for the resume" in (d / "user_profile.md").read_text()
    assert "Never store passwords" in (d / "user_profile.md").read_text()


def test_seed_never_overwrites_user_edits(tmp_path):
    d = seed_memory(tmp_path / "mem")
    (d / "user_profile.md").write_text("# mine\nKafka: used at Acme")
    seed_memory(tmp_path / "mem")
    assert (d / "user_profile.md").read_text() == "# mine\nKafka: used at Acme"


def test_memory_is_mounted_read_write_outside_output(tmp_path):
    resume = tmp_path / "cv.docx"
    resume.write_bytes(RESUME)
    out = tmp_path / "out"
    mem = seed_memory(tmp_path / "mem")
    mounts = {m.container_path: m for m in sandbox_mounts(prepare_resume(resume, out), out, mem)}
    assert mounts[MEMORY_MOUNT].read_only is False
    assert mounts[MEMORY_MOUNT].host_path == mem
    assert out not in mem.parents  # cleanup of /output can never reach it
    no_memory = {m.container_path for m in sandbox_mounts(prepare_resume(resume, out), out)}
    assert MEMORY_MOUNT not in no_memory


def test_which_agent_loads_what():
    assert MAIN_MEMORY == [USER_PROFILE, AGENT_NOTES]
    assert SUBAGENT_MEMORY["job-search"] == [USER_PROFILE, AGENT_NOTES]
    assert SUBAGENT_MEMORY["job-matcher"] == [USER_PROFILE]
    assert SUBAGENT_MEMORY["resume-builder"] == [USER_PROFILE]
    assert SUBAGENT_MEMORY["ats-reviewer"] == []  # independent scoring


def test_subagents_get_read_only_memory(model):
    subagents = {s["name"]: s for s in build_subagents(model, DockerSandboxBackend(), [])}
    for name, sources in SUBAGENT_MEMORY.items():
        memory = [m for m in subagents[name]["middleware"] if isinstance(m, MemoryMiddleware)]
        if sources:
            [mw] = memory
            assert mw.sources == sources
            assert "You do not edit memory files" in mw.system_prompt
        else:
            assert memory == []


def test_memory_off(model):
    subagents = build_subagents(model, DockerSandboxBackend(), [], memory=False)
    assert not any(isinstance(m, MemoryMiddleware) for s in subagents for m in s["middleware"])
    graph = create_jobhunter_agent(
        model=model,
        backend=DockerSandboxBackend(),
        browser_tools=[],
        resume_path="/input/cv.docx",
        memory=False,
    )
    assert "MemoryMiddleware.before_agent" not in graph.nodes
    assert "memory: off" in prompts.jobhunter_prompt("/input/cv.docx", memory=False)


def test_orchestrator_loads_memory(model):
    graph = create_jobhunter_agent(
        model=model, backend=DockerSandboxBackend(), browser_tools=[], resume_path="/input/cv.docx"
    )
    assert "MemoryMiddleware.before_agent" in graph.nodes
    assert "follow the `agent-memory` skill" in prompts.jobhunter_prompt("/input/cv.docx")


def test_subagent_memory_prompt_has_placeholder():
    assert "{agent_memory}" in SUBAGENT_MEMORY_PROMPT
    assert "Memory notes" in SUBAGENT_MEMORY_PROMPT


def test_builder_uses_confirmed_facts_instead_of_asking():
    assert "from your profile" in prompts.RESUME_BUILDER_PROMPT
    assert "Memory notes" in prompts.JOB_SEARCH_PROMPT


def test_agent_memory_skill():
    text = (SKILLS_DIR / "orchestrator" / "agent-memory" / "SKILL.md").read_text()
    assert USER_PROFILE in text and AGENT_NOTES in text
    assert "Never store passwords" in text
    assert "BEFORE `output-cleanup`" in text
    routing = (SKILLS_DIR / "orchestrator" / "agent-orchestration" / "SKILL.md").read_text()
    assert "`agent-memory`" in routing


def test_templates_have_no_secrets_slots():
    for text in TEMPLATES.values():
        assert "password:" not in text.lower() and "api key:" not in text.lower()
