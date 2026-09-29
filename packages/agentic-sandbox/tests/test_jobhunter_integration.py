"""End-to-end: the builder <-> ATS-reviewer loop on a real Docker sandbox.

The model is a fake that replays fixed tool calls (no API key, no cost,
deterministic). Everything else is real: the deep agent graph, `task` hand-offs
to the resume-builder and ats-reviewer subagents, skills loaded from /skills
inside the container, scripts written with `write_file` and run with `execute`
in the sandbox, and all files landing on the host through the /output mount.

One job, two rounds: round 1 is a light edit that scores below target; the
reviewer's feedback drives round 2, which scores higher; the orchestrator keeps
the best round and writes ats_history.json.
"""

from __future__ import annotations

import json

import pytest
from genai_agentic_sandbox.agent import create_jobhunter_agent
from genai_agentic_sandbox.builder import SandboxImageBuilder
from genai_agentic_sandbox.main import SKILLS_DIR, prepare_resume, sandbox_mounts
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from .docx_samples import PALETTE, RESUME, comments, render, runs_info, skill_code

pytestmark = pytest.mark.integration

RESUME_SKILL = (SKILLS_DIR / "resume" / "docx-tracked-revisions" / "SKILL.md").read_text()
HELPERS = skill_code(RESUME_SKILL, "tracked-change helpers")
SCORER = skill_code(
    (SKILLS_DIR / "ats" / "ats-scoring" / "SKILL.md").read_text(), "ats-score reference"
)

JOB_DIR = "/output/resume/1-acme-backend"
STEM, SLUG = "Jane Resume", "acme-backend"
JOB = {
    "required_skills": ["Python", "FastAPI", "Docker"],
    "nice_to_have": [],
    "keywords": ["REST", "latency", "Senior"],
}

ROUND_CHANGES = {
    1: """
    change("title", lambda: track_replace(P[1], "Python developer", "Senior Python developer"))
""",
    2: """
    change("title", lambda: track_replace(P[1], "Python developer", "Senior Python developer"))
    change("fastapi", lambda: track_insert(P[3], ", FastAPI", after="Django"))
    change("summary heading", lambda: insert_paragraph_after(P[0], "Summary", like=P[2]))
    change("latency bullet", lambda: insert_paragraph_after(P[7], "Cut API latency 40% on REST services.", like=P[7]))
    change("flag dates", lambda: flag_wrong(doc, P[6], "2020–2025", "2020–Present", "end date in the future"))
""",
}


def builder_script(k: int) -> str:
    return (
        HELPERS
        + f"""
from pathlib import Path
SRC = "/input/{STEM}.docx"


def build(mode, out):
    global REVIEW
    REVIEW = mode != "final"
    doc = open_doc(SRC)
    P = paragraphs(doc)
    add_legend(doc, P)
    results = []

    def change(label, fn):
        try:
            fn()
            results.append(f"OK   {{label}}")
        except Exception as exc:
            results.append(f"FAIL {{label}}: {{exc}}")
{ROUND_CHANGES[k]}
    if mode == "review":
        enable_track_changes(doc)
    elif mode == "redline":
        to_redline(doc)
    else:
        accept_all(doc)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    doc.save(out)
    return results


base = "{JOB_DIR}/v{k}/{STEM}_{SLUG}"
print("\\n".join(build("review", base + "_review.docx")))
build("redline", base + "_redline.docx")
build("final", base + "_final.docx")
print("CHECK reject-all == original:", render(base + "_review.docx", "original") == render(SRC, "original"))
"""
    )


# The reviewer's merge step: script score + verdict -> ats/round-<k>.json
REVIEW_PY = """
import json, sys
k, target = int(sys.argv[1]), float(sys.argv[2])
job = "/output/resume/1-acme-backend"
s = json.load(open(f"{job}/ats/score-{k}.json"))
prev = json.load(open(f"{job}/ats/round-{k-1}.json"))["score"] if k > 1 else None
verdict = "done" if s["score"] >= target else "improve"
json.dump({"round": k, "score": s["score"], "previous_score": prev, "breakdown": s["breakdown"],
           "missing_required": s["missing_required"], "missing_keywords": s["missing_keywords"],
           "issues": s["issues"], "quality_notes": "", "verdict": verdict,
           "reason": f"score {s['score']} vs target {target}"},
          open(f"{job}/ats/round-{k}.json", "w"), indent=2)
print("round", k, "score", s["score"], verdict)
"""

FINISH = f"""
import json, shutil
job = "{JOB_DIR}"
rounds = [json.load(open(f"{{job}}/ats/round-{{k}}.json")) for k in (1, 2)]
best = max(rounds, key=lambda r: (r["score"], r["round"]))["round"]
for kind in ("redline", "review", "final"):
    shutil.copy(f"{{job}}/v{{best}}/{STEM}_{SLUG}_{{kind}}.docx", f"{{job}}/{STEM}_{SLUG}_{{kind}}.docx")
json.dump({{"target": 85, "best_round": best,
            "rounds": [{{"round": r["round"], "score": r["score"], "verdict": r["verdict"]}} for r in rounds]}},
          open(f"{{job}}/ats_history.json", "w"), indent=2)
print("best round", best)
"""


class ScriptedModel(BaseChatModel):
    """Replays `script` one message per call and records what it was sent."""

    script: list = Field(default_factory=list)
    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


class Calls:
    def __init__(self):
        self.n = 0

    def __call__(self, name: str, args: dict) -> AIMessage:
        self.n += 1
        return AIMessage(
            content="", tool_calls=[{"name": name, "args": args, "id": f"call_{self.n}"}]
        )


def subagent_turns(call: Calls, steps: list[str], work: list, answer: str) -> list:
    """A subagent's turns done properly: plan with todos, work, complete them, answer."""
    plan = [
        {"content": s, "status": "in_progress" if i == 0 else "pending"}
        for i, s in enumerate(steps)
    ]
    done = [dict(t, status="completed") for t in plan]
    return [
        call("write_todos", {"todos": plan}),
        *work,
        call("write_todos", {"todos": done}),
        AIMessage(content=answer),
    ]


def test_builder_reviewer_loop_improves_and_keeps_the_best_round(tmp_path):
    user_resume = tmp_path / "home" / f"{STEM}.docx"
    user_resume.parent.mkdir()
    user_resume.write_bytes(RESUME)
    out = tmp_path / "out"
    copy = prepare_resume(user_resume, out)
    call = Calls()

    plan = [
        {"content": f"job 1 round {k} {step}", "status": "pending"}
        for k in (1, 2)
        for step in ("build", "ATS review")
    ] + [{"content": "finalise job 1", "status": "pending"}]
    script = [  # the orchestrator writes its plan to disk, then mirrors it in todos
        call("write_todos", {"todos": plan}),
    ]
    for k in (1, 2):
        feedback = f" Feedback: {JOB_DIR}/ats/round-{k - 1}.json." if k > 1 else ""
        if k == 1:
            build = [
                call(
                    "write_file",
                    {"file_path": f"{JOB_DIR}/revise_resume.py", "content": builder_script(1)},
                )
            ]
        else:  # round 2 extends the same script from the feedback
            build = [
                call(
                    "edit_file",
                    {
                        "file_path": f"{JOB_DIR}/revise_resume.py",
                        "old_string": ROUND_CHANGES[1].strip("\n"),
                        "new_string": ROUND_CHANGES[2].strip("\n"),
                    },
                ),
                call(
                    "edit_file",
                    {
                        "file_path": f"{JOB_DIR}/revise_resume.py",
                        "old_string": f'base = "{JOB_DIR}/v1/',
                        "new_string": f'base = "{JOB_DIR}/v2/',
                    },
                ),
            ]
        build.append(call("execute", {"command": f"python {JOB_DIR}/revise_resume.py"}))
        review = []
        if k == 1:
            review += [
                call(
                    "write_file",
                    {"file_path": f"{JOB_DIR}/ats/job.json", "content": json.dumps(JOB)},
                ),
                call("write_file", {"file_path": f"{JOB_DIR}/ats/ats_score.py", "content": SCORER}),
                call("write_file", {"file_path": f"{JOB_DIR}/ats/review.py", "content": REVIEW_PY}),
            ]
        review.append(
            call(
                "execute",
                {
                    "command": f"cd {JOB_DIR}/ats && python ats_score.py '../v{k}/{STEM}_{SLUG}_final.docx' "
                    f"job.json score-{k}.json && python review.py {k} 85"
                },
            )
        )
        script += [
            call(
                "task",
                {
                    "subagent_type": "resume-builder",
                    "description": f"Job 1 acme-backend, round {k}, folder {JOB_DIR}/.{feedback}",
                },
            ),
            *subagent_turns(
                call,
                ["write / extend script", "run and verify"],
                build,
                f"Round {k} written to {JOB_DIR}/v{k}/",
            ),
            call(
                "task",
                {
                    "subagent_type": "ats-reviewer",
                    "description": f"Job 1 acme-backend, round {k}, target 85, "
                    f"{JOB_DIR}/v{k}/{STEM}_{SLUG}_final.docx",
                },
            ),
            *subagent_turns(call, ["score", "write round file"], review, f"Round {k} scored"),
            call("read_file", {"file_path": f"{JOB_DIR}/ats/round-{k}.json"}),
        ]
    script += [
        call("write_file", {"file_path": f"{JOB_DIR}/finish.py", "content": FINISH}),
        call("execute", {"command": f"python {JOB_DIR}/finish.py"}),
        call("write_todos", {"todos": [dict(t, status="completed") for t in plan]}),
        AIMessage(content="Done: best round kept"),
    ]
    model = ScriptedModel(script=script)

    with SandboxImageBuilder().backend(mounts=sandbox_mounts(copy, out)) as backend:
        agent = create_jobhunter_agent(
            model=model,
            backend=backend,
            browser_tools=[],
            resume_path=f"/input/{STEM}.docx",
            top_jobs=1,
            target_score=85,
            max_rounds=3,
            cleanup=False,  # this test inspects the working files
        )
        result = agent.invoke(
            {"messages": [{"role": "user", "content": "Tailor my resume for the best job"}]},
            config={"recursion_limit": 150},
        )

    assert model.script == []
    assert result["messages"][-1].content == "Done: best round kept"

    # The right skills reached the right agents (loaded from /skills in the container).
    systems = [str(m[0].content) for m in model.seen]
    assert "agent-orchestration" in systems[0] and "resume-optimization" in systems[0]
    assert "ATS target score: 85" in systems[0] and "do NOT run `output-cleanup`" in systems[0]

    # The todo list is the only plan: no plan files are written.
    assert not (out / "plan").exists()

    # Plans ran to the end: everything completed, no agent had to be reminded.
    assert all(t["status"] == "completed" for t in result["todos"])
    assert not [m for m in result["messages"] if "[todo check]" in str(m.content)]
    assert any("docx-tracked-revisions" in s and "ats-scoring" not in s for s in systems)
    assert any("ats-scoring" in s and "docx-tracked-revisions" not in s for s in systems)

    job = out / "resume" / "1-acme-backend"
    rounds = [json.loads((job / "ats" / f"round-{k}.json").read_text()) for k in (1, 2)]
    assert rounds[0]["verdict"] == "improve"
    assert rounds[1]["score"] > rounds[0]["score"]
    assert rounds[1]["previous_score"] == rounds[0]["score"]
    assert (
        "FastAPI" in rounds[0]["missing_required"]
        and "FastAPI" not in rounds[1]["missing_required"]
    )

    history = json.loads((job / "ats_history.json").read_text())
    assert history["best_round"] == 2
    assert [r["round"] for r in history["rounds"]] == [1, 2]

    original = render(RESUME, "original")
    for k in (1, 2):
        for kind in ("redline", "review", "final"):
            assert (job / f"v{k}" / f"{STEM}_{SLUG}_{kind}.docx").exists(), (k, kind)
        assert (
            render((job / f"v{k}" / f"{STEM}_{SLUG}_review.docx").read_bytes(), "original")
            == original
        )
    for kind in ("redline", "review", "final"):  # best round copied up
        top = (job / f"{STEM}_{SLUG}_{kind}.docx").read_bytes()
        assert top == (job / "v2" / f"{STEM}_{SLUG}_{kind}.docx").read_bytes()

    final = render((job / f"{STEM}_{SLUG}_final.docx").read_bytes())
    assert final[1] == "Summary"
    assert "Python, Django, FastAPI, SQL" in final
    assert "Cut API latency 40% on REST services." in final
    colours = {
        r["text"]: r["color"] for r in runs_info((job / f"{STEM}_{SLUG}_redline.docx").read_bytes())
    }
    assert (
        colours[", FastAPI"] == PALETTE["added"]
        and colours["Python developer"] == PALETTE["removed"]
    )
    assert colours["2020–2025"] == PALETTE["flag"]
    assert comments((job / f"{STEM}_{SLUG}_review.docx").read_bytes())[0].startswith(
        "Review colours"
    )

    # One scoring script, reused for both rounds; inputs untouched.
    assert sorted(p.name for p in (job / "ats").iterdir()) == [
        "ats_score.py",
        "job.json",
        "review.py",
        "round-1.json",
        "round-2.json",
        "score-1.json",
        "score-2.json",
    ]
    assert user_resume.read_bytes() == RESUME and copy.read_bytes() == RESUME


ORCH = SKILLS_DIR / "orchestrator"
CLEANUP = skill_code((ORCH / "output-cleanup" / "SKILL.md").read_text(), "cleanup reference")
REPORT = skill_code((ORCH / "ats-score-only" / "SKILL.md").read_text(), "ats-report reference")
SCORE_ONLY_MERGE = """
import json
d = "/output/ats/acme-backend"
s = json.load(open(f"{d}/score-1.json"))
s.update({"round": 1, "mode": "score-only", "verdict": "n/a", "quality_notes": "Add a Summary."})
json.dump(s, open(f"{d}/round-1.json", "w"), indent=2)
print("score", s["score"])
"""


def test_ats_score_only_then_cleanup_leaves_only_the_report(tmp_path):
    """Score-only workflow: no editing; the report .docx is the only file left."""
    import io

    import docx

    user_resume = tmp_path / "home" / f"{STEM}.docx"
    user_resume.parent.mkdir()
    user_resume.write_bytes(RESUME)
    out = tmp_path / "out"
    copy = prepare_resume(user_resume, out)
    call = Calls()
    d = "/output/ats/acme-backend"
    plan = [
        {"content": c, "status": "pending"}
        for c in ("ATS score vs Acme job", "build report", "cleanup")
    ]
    job = {**JOB, "title": "Backend Engineer", "company": "Acme"}
    model = ScriptedModel(
        script=[
            call("write_todos", {"todos": plan}),
            call(
                "task",
                {
                    "subagent_type": "ats-reviewer",
                    "description": f'mode "score-only", resume /input/{STEM}.docx, job acme-backend, folder {d}/',
                },
            ),
            # reviewer (score-only): plans, scores (no resume edits), completes its todos
            *subagent_turns(
                call,
                ["write job.json + scorer", "score the resume as is"],
                [
                    call("write_file", {"file_path": f"{d}/job.json", "content": json.dumps(job)}),
                    call("write_file", {"file_path": f"{d}/ats_score.py", "content": SCORER}),
                    call("write_file", {"file_path": f"{d}/merge.py", "content": SCORE_ONLY_MERGE}),
                    call(
                        "execute",
                        {
                            "command": f"cd {d} && python ats_score.py '/input/{STEM}.docx' "
                            "job.json score-1.json && python merge.py"
                        },
                    ),
                ],
                "scored",
            ),
            # orchestrator: report, then cleanup (dry run, then apply)
            call("write_file", {"file_path": "/output/ats/build_report.py", "content": REPORT}),
            call("execute", {"command": "python /output/ats/build_report.py"}),
            call("write_file", {"file_path": "/tmp/cleanup_output.py", "content": CLEANUP}),
            call("execute", {"command": "python /tmp/cleanup_output.py"}),
            call("execute", {"command": "python /tmp/cleanup_output.py --apply"}),
            call("write_todos", {"todos": [dict(t, status="completed") for t in plan]}),
            AIMessage(content="Your resume scores X for Acme. Report: /output/ats/ats_report.docx"),
        ]
    )
    with SandboxImageBuilder().backend(mounts=sandbox_mounts(copy, out)) as backend:
        agent = create_jobhunter_agent(
            model=model, backend=backend, browser_tools=[], resume_path=f"/input/{STEM}.docx"
        )
        result = agent.invoke(
            {
                "messages": [
                    {"role": "user", "content": "What is my ATS score for the Acme backend job?"}
                ]
            },
            config={"recursion_limit": 80},
        )

    assert model.script == []
    main_system = str(model.seen[0][0].content)
    assert "ats-score-only" in main_system and "output-cleanup" in main_system
    assert "run the `output-cleanup` skill as the last step" in main_system
    assert all(t["status"] == "completed" for t in result["todos"])

    # The dry run reported the plan; nothing was deleted until --apply.
    dry_run = next(
        str(m[-1].content)
        for m in model.seen
        if "dry run - re-run with --apply" in str(m[-1].content)
    )
    assert "ats/ats_report.docx" in dry_run.split("DELETE")[0]

    # Only the deliverable (and the read-only resume copy) is left.
    left = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert left == [
        "ats/ats_report.docx",
        f"original/{STEM}.docx",
    ]
    report = docx.Document(io.BytesIO((out / "ats" / "ats_report.docx").read_bytes()))
    text = "\n".join(p.text for p in report.paragraphs)
    assert "Backend Engineer - score" in text and "Missing required skills: FastAPI" in text

    # Nothing was edited.
    assert user_resume.read_bytes() == RESUME and copy.read_bytes() == RESUME


MEMORY_RUN_PLAN = [
    {"content": c, "status": "pending"}
    for c in ("1. build round 1", "2. review round 1", "3. save memory", "4. cleanup")
]


def test_memory_persists_across_runs_and_reaches_the_right_agents(tmp_path):
    """Run 1 learns; run 2 (new agent, new sandbox, new thread) starts with it."""
    from genai_agentic_sandbox.memory import seed_memory

    user_resume = tmp_path / "home" / f"{STEM}.docx"
    user_resume.parent.mkdir()
    user_resume.write_bytes(RESUME)
    out = tmp_path / "out"
    copy = prepare_resume(user_resume, out)
    # Default layout: a visible memories/ folder inside the output folder.
    memory_dir = seed_memory(out / "memories", legacy=None)
    profile = memory_dir / "user_profile.md"
    # The user answered last run's question by hand:
    profile.write_text(
        profile.read_text().replace(
            "## Confirmed facts for the resume",
            "## Confirmed facts for the resume\n- Kafka: used at Acme 2023-24 (FACT-7Q)",
        )
    )

    call = Calls()
    run1 = ScriptedModel(
        script=[
            call("write_todos", {"todos": MEMORY_RUN_PLAN}),
            # user states a preference -> orchestrator saves it right away
            call(
                "edit_file",
                {
                    "file_path": "/memories/user_profile.md",
                    "old_string": "## Job preferences",
                    "new_string": "## Job preferences\n- Only remote roles (PREF-3X)",
                },
            ),
            call("task", {"subagent_type": "resume-builder", "description": "round 1 for job 1"}),
            *subagent_turns(
                call,
                ["build round 1"],
                # a subagent tries to write memory itself -> refused (read-only)
                [
                    call(
                        "edit_file",
                        {
                            "file_path": "/memories/user_profile.md",
                            "old_string": "## Links",
                            "new_string": "## Links\n- SUBAGENT-WROTE-THIS",
                        },
                    )
                ],
                "Built.\nMemory notes:\n- 'Summary' heading raised sections score (NOTE-5Z)",
            ),
            call("task", {"subagent_type": "ats-reviewer", "description": "score round 1"}),
            AIMessage(content="score 80"),
            # end of run: curate the subagent's memory note
            call(
                "edit_file",
                {
                    "file_path": "/memories/agent_notes.md",
                    "old_string": "## Resume changes that moved the ATS score",
                    "new_string": "## Resume changes that moved the ATS score\n- Summary heading: +sections (NOTE-5Z)",
                },
            ),
            # attempts to wipe memory -> refused
            call("execute", {"command": "rm -rf /output/memories"}),
            call("delete", {"file_path": "/memories/agent_notes.md"}),
            # the real cleanup keeps memories/
            call("write_file", {"file_path": "/tmp/cleanup_output.py", "content": CLEANUP}),
            call("execute", {"command": "python /tmp/cleanup_output.py --apply"}),
            call("write_todos", {"todos": [dict(t, status="completed") for t in MEMORY_RUN_PLAN]}),
            AIMessage(content="done"),
        ]
    )
    with SandboxImageBuilder().backend(mounts=sandbox_mounts(copy, out, memory_dir)) as backend:
        agent = create_jobhunter_agent(
            model=run1,
            backend=backend,
            browser_tools=[],
            resume_path=f"/input/{STEM}.docx",
            cleanup=False,
        )
        agent.invoke(
            {
                "messages": [
                    {"role": "user", "content": "Tailor my resume. I only want remote roles."}
                ]
            },
            config={"recursion_limit": 60},
        )
    assert run1.script == []

    systems = [str(m[0].content) for m in run1.seen]
    main = systems[0]
    builder = next(x for x in systems if "You are the resume builder" in x)
    reviewer = next(x for x in systems if "You are the ATS reviewer" in x)
    assert "You are JobHunter" in main and "FACT-7Q" in main
    assert (
        "You are the resume builder" in builder and "FACT-7Q" in builder
    )  # confirmed fact reaches the builder
    assert "You do not edit memory files" in builder
    assert (
        "You are the ATS reviewer" in reviewer and "FACT-7Q" not in reviewer
    )  # independent scoring
    assert "PREF-3X" in profile.read_text()
    assert "NOTE-5Z" in (memory_dir / "agent_notes.md").read_text()
    # The subagent's write and both wipe attempts were refused; cleanup kept memory.
    assert "SUBAGENT-WROTE-THIS" not in profile.read_text()
    refusals = [str(m[-1].content) for m in run1.seen if "Error:" in str(m[-1].content)]
    assert any("read-only for you" in r for r in refusals)
    assert sum("permanent" in r for r in refusals) == 2
    assert sorted(p.name for p in memory_dir.iterdir()) == ["agent_notes.md", "user_profile.md"]
    assert (out / "memories" / "user_profile.md").read_text() == profile.read_text()

    # Run 2: a fresh agent, sandbox and thread start with what run 1 learned.
    run2 = ScriptedModel(script=[AIMessage(content="hello again")])
    with SandboxImageBuilder().backend(mounts=sandbox_mounts(copy, out, memory_dir)) as backend:
        agent = create_jobhunter_agent(
            model=run2, backend=backend, browser_tools=[], resume_path=f"/input/{STEM}.docx"
        )
        agent.invoke({"messages": [{"role": "user", "content": "hi"}]})
    first_prompt = str(run2.seen[0][0].content)
    assert "PREF-3X" in first_prompt and "NOTE-5Z" in first_prompt and "FACT-7Q" in first_prompt
