"""Long-term memory: seeding, mounting, and which agent loads which file."""

from __future__ import annotations

import pytest
from deepagents.middleware.memory import MemoryMiddleware
from genai_agentic_sandbox import prompts
from genai_agentic_sandbox.agent import (
    MAIN_MEMORY,
    SUBAGENT_MEMORY,
    create_jobhunter_agent,
    declarative_specs,
)
from genai_agentic_sandbox.main import SKILLS_DIR, prepare_resume, sandbox_mounts
from genai_agentic_sandbox.memory import (
    AGENT_NOTES,
    MEMORY_DIRNAME,
    MEMORY_MOUNT,
    SUBAGENT_MEMORY_PROMPT,
    TEMPLATES,
    USER_PROFILE,
    MemoryGuardMiddleware,
    describe_memory,
    seed_memory,
)
from genai_agentic_sandbox.sandbox.docker import DockerSandboxBackend
from langchain_openai import ChatOpenAI

from .docx_samples import RESUME


@pytest.fixture
def model():
    return ChatOpenAI(model="gpt-5.5", api_key="sk-test")


def test_seed_creates_templates(tmp_path):
    d = seed_memory(tmp_path / "mem", legacy=None)
    assert sorted(p.name for p in d.iterdir()) == ["agent_notes.md", "user_profile.md"]
    assert "## Confirmed facts for the resume" in (d / "user_profile.md").read_text()
    assert "Never store passwords" in (d / "user_profile.md").read_text()


def test_seed_never_overwrites_user_edits(tmp_path):
    d = seed_memory(tmp_path / "mem", legacy=None)
    (d / "user_profile.md").write_text("# mine\nKafka: used at Acme")
    seed_memory(tmp_path / "mem", legacy=None)
    assert (d / "user_profile.md").read_text() == "# mine\nKafka: used at Acme"


def test_seed_copies_memory_from_the_old_hidden_folder(tmp_path):
    legacy = tmp_path / "home" / ".jobhunter" / "memory"
    legacy.mkdir(parents=True)
    (legacy / "user_profile.md").write_text("# old profile\nOnly remote roles")
    d = seed_memory(tmp_path / "out" / "memories", legacy=legacy)
    assert (d / "user_profile.md").read_text() == "# old profile\nOnly remote roles"  # copied
    assert (d / "agent_notes.md").read_text() == TEMPLATES[
        "agent_notes.md"
    ]  # not in legacy: template
    assert (legacy / "user_profile.md").exists()  # copied, not moved


def test_default_memory_is_a_visible_folder_in_the_output(tmp_path):
    assert MEMORY_DIRNAME == "memories" and not MEMORY_DIRNAME.startswith(".")
    d = seed_memory(tmp_path / "out" / MEMORY_DIRNAME, legacy=None)
    text = describe_memory(d)
    assert str(d / "user_profile.md") in text and "bytes" in text


def test_memory_is_mounted_read_write(tmp_path):
    resume = tmp_path / "cv.docx"
    resume.write_bytes(RESUME)
    out = tmp_path / "out"
    mem = seed_memory(out / MEMORY_DIRNAME, legacy=None)
    mounts = {m.container_path: m for m in sandbox_mounts(prepare_resume(resume, out), out, mem)}
    assert mounts[MEMORY_MOUNT].read_only is False
    assert mounts[MEMORY_MOUNT].host_path == mem
    no_memory = {m.container_path for m in sandbox_mounts(prepare_resume(resume, out), out)}
    assert MEMORY_MOUNT not in no_memory


# -- guard ----------------------------------------------------------------------------


def blocked(guard, name, **args):
    return guard._blocked({"name": name, "args": args}) is not None


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /memories",
        "rm /output/memories/user_profile.md",
        "mv /memories/agent_notes.md /tmp",
        "find /output/memories -delete",
        "rm -rf /output",
        "rm -rf /output/*",
        "python -c 'import shutil; shutil.rmtree(\"/output/memories\")'",
        ": > /memories/user_profile.md",
    ],
)
def test_nobody_can_delete_or_wipe_memory(read_only, command):
    assert blocked(MemoryGuardMiddleware(read_only=read_only), "execute", command=command)


@pytest.mark.parametrize("read_only", [False, True])
def test_delete_tool_refused_on_memory(read_only):
    guard = MemoryGuardMiddleware(read_only=read_only)
    assert blocked(guard, "delete", file_path="/memories/user_profile.md")
    assert blocked(guard, "delete", file_path="/output/memories/agent_notes.md")
    assert not blocked(guard, "delete", file_path="/output/jobs/jobs.json")


def test_orchestrator_may_edit_memory_subagents_may_not():
    orchestrator, subagent = MemoryGuardMiddleware(), MemoryGuardMiddleware(read_only=True)
    for tool in ("edit_file", "write_file"):
        assert not blocked(orchestrator, tool, file_path="/memories/user_profile.md")
        assert blocked(subagent, tool, file_path="/memories/user_profile.md")
    assert blocked(subagent, "execute", command="echo x >> /memories/agent_notes.md")
    assert blocked(subagent, "execute", command="cp notes.md /output/memories/agent_notes.md")


@pytest.mark.parametrize(
    "command",
    [
        "cat /memories/user_profile.md",
        "ls /output",
        "rm -rf /output/jobs",
        "python /tmp/cleanup_output.py --apply",
        "grep -r Kafka /memories",
    ],
)
def test_normal_work_is_not_blocked(command):
    assert not blocked(MemoryGuardMiddleware(read_only=True), "execute", command=command)


def test_every_agent_has_the_guard(model):
    subagents = declarative_specs(model, DockerSandboxBackend(), [])
    for s in subagents.values():
        [guard] = [m for m in s["middleware"] if isinstance(m, MemoryGuardMiddleware)]
        assert guard.read_only is True, s["name"]
    graph = create_jobhunter_agent(
        model=model, backend=DockerSandboxBackend(), browser_tools=[], resume_path="/input/cv.docx"
    )
    assert graph is not None


def test_guard_returns_an_error_instead_of_running(model):
    ran = []
    msg = MemoryGuardMiddleware().wrap_tool_call(
        type(
            "R",
            (),
            {"tool_call": {"name": "execute", "args": {"command": "rm -rf /memories"}, "id": "c1"}},
        )(),
        lambda r: ran.append(r),
    )
    assert ran == [] and msg.status == "error" and "permanent" in msg.content


def test_which_agent_loads_what():
    assert MAIN_MEMORY == [USER_PROFILE, AGENT_NOTES]
    assert SUBAGENT_MEMORY["job-search"] == [USER_PROFILE, AGENT_NOTES]
    assert SUBAGENT_MEMORY["job-matcher"] == [USER_PROFILE]
    assert SUBAGENT_MEMORY["resume-builder"] == [USER_PROFILE]
    assert SUBAGENT_MEMORY["ats-reviewer"] == []  # independent scoring


def test_subagents_get_read_only_memory(model):
    subagents = declarative_specs(model, DockerSandboxBackend(), [])
    for name, sources in SUBAGENT_MEMORY.items():
        if name not in subagents:  # job-optimizer is compiled; it loads no memory
            assert sources == []
            continue
        memory = [m for m in subagents[name]["middleware"] if isinstance(m, MemoryMiddleware)]
        if sources:
            [mw] = memory
            assert mw.sources == sources
            assert "You do not edit memory files" in mw.system_prompt
        else:
            assert memory == []


def test_memory_off(model):
    subagents = list(declarative_specs(model, DockerSandboxBackend(), [], memory=False).values())
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
