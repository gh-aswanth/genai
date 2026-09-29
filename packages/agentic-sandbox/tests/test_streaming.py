"""Event stream v2 -> typed events -> listeners -> Rich rendering."""

from __future__ import annotations

import asyncio
import re
import types

import pytest
from deepagents.backends import StateBackend
from genai_agentic_sandbox.agent import create_jobhunter_agent
from genai_agentic_sandbox.main import _run_turn
from genai_agentic_sandbox.streaming import (
    KINDS,
    AgentEvent,
    EventStream,
    RichRenderer,
    short_lane,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field
from rich.console import Console

USAGE = {
    "input_tokens": 2048,
    "output_tokens": 12,
    "total_tokens": 2060,
    "input_token_details": {"cache_read": 1536},
}


class StreamingModel(BaseChatModel):
    """Scripted model that streams: tool calls in one chunk, text word by word."""

    script: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-stream"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        message = self.script.pop(0)
        if message.tool_calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="", tool_calls=message.tool_calls, usage_metadata=USAGE
                )
            )
            return
        for word in message.content.split(" "):
            yield ChatGenerationChunk(message=AIMessageChunk(content=word + " "))


def call(name, args, n):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c{n}"}])


FINAL = "Your resume scores **72.5**. Biggest gain: add *Kafka* experience."


def scripted_graph():
    model = StreamingModel(
        script=[
            call("write_todos", {"todos": [{"content": "1. score", "status": "in_progress"}]}, 1),
            call(
                "task",
                {"subagent_type": "ats-reviewer", "description": "Score the resume (score-only)."},
                2,
            ),
            # --- ats-reviewer (subagent) ---
            call(
                "write_todos", {"todos": [{"content": "1. run scorer", "status": "in_progress"}]}, 3
            ),
            call(
                "write_file", {"file_path": "/output/ats/score.py", "content": "print(72.5)\n"}, 4
            ),
            call(
                "execute", {"command": "python /output/ats/score.py"}, 5
            ),  # no sandbox here -> error
            call(
                "write_todos", {"todos": [{"content": "1. run scorer", "status": "completed"}]}, 6
            ),
            AIMessage(content="Score 72.5/100; missing: Kafka."),
            # --- back in the orchestrator ---
            call("write_todos", {"todos": [{"content": "1. score", "status": "completed"}]}, 7),
            AIMessage(content=FINAL),
        ]
    )
    graph = create_jobhunter_agent(
        model=model,
        backend=StateBackend(),
        browser_tools=[],
        resume_path="/input/cv.docx",
        memory=False,
    )
    return graph, model


INPUT = {"messages": [{"role": "user", "content": "score my resume"}]}


async def collect() -> list[AgentEvent]:
    graph, model = scripted_graph()
    events = [e async for e in EventStream().events(graph, INPUT)]
    assert model.script == []
    return events


@pytest.fixture(scope="module")
def events() -> list[AgentEvent]:
    return asyncio.run(collect())


def of(events, kind, agent=None):
    return [e for e in events if e.kind == kind and (agent is None or e.agent == agent)]


# -- classification on a real run -----------------------------------------------------------


def test_orchestrator_and_subagent_runs(events):
    [main] = of(events, "agent_start", "jobhunter")
    assert main.depth == 0
    [sub] = of(events, "agent_start", "ats-reviewer")
    assert sub.depth == 1
    assert [e.agent for e in of(events, "agent_end")] == ["ats-reviewer", "jobhunter"]


def test_subagent_calls(events):
    [start] = of(events, "subagent_start")
    assert (start.agent, start.data["subagent"]) == ("jobhunter", "ats-reviewer")
    assert start.data["task"] == "Score the resume (score-only)."
    [end] = of(events, "subagent_end")
    assert end.data["subagent"] == "ats-reviewer" and "72.5/100" in end.data["output"]


def test_tool_calls_are_attributed_to_the_subagent(events):
    starts = of(events, "tool_start", "ats-reviewer")
    assert [e.name for e in starts] == ["write_file", "execute"]
    write = starts[0]
    assert write.depth == 1
    assert write.data["args"]["file_path"] == "/output/ats/score.py"
    [ok] = of(events, "tool_end", "ats-reviewer")
    assert ok.name == "write_file"
    [error] = of(events, "tool_error", "ats-reviewer")
    assert error.name == "execute" and "Execution not available" in error.data["output"]
    assert not any(e.name in ("task", "write_todos") for e in of(events, "tool_start"))


def test_todos_per_agent(events):
    main = [e.data["todos"][0]["status"] for e in of(events, "todos", "jobhunter")]
    sub = [e.data["todos"][0]["status"] for e in of(events, "todos", "ats-reviewer")]
    assert main == ["in_progress", "completed"] and sub == ["in_progress", "completed"]


def test_model_calls_tokens_and_usage(events):
    assert len(of(events, "model_start", "jobhunter")) == 4
    assert len(of(events, "model_start", "ats-reviewer")) == 5
    tokens = "".join(e.data["text"] for e in of(events, "model_token", "jobhunter"))
    assert tokens.strip() == FINAL
    ends = of(events, "model_end", "jobhunter")
    assert ends[-1].data["text"].strip() == FINAL and not ends[-1].data["tool_calls"]
    assert ends[0].data["tool_calls"][0]["name"] == "write_todos"
    assert ends[0].data["usage"]["input_token_details"]["cache_read"] == 1536


def test_graph_nodes(events):
    names = {e.name for e in of(events, "node_start")}
    assert {"model", "tools"} <= names
    assert any("Middleware" in n for n in names)


# -- dispatch ---------------------------------------------------------------------------------


def test_listeners_per_kind_sync_and_async():
    graph, _ = scripted_graph()
    stream, seen = EventStream(), []

    @stream.on("subagent_start")
    def sync_listener(event):
        seen.append(("sync", event.data["subagent"]))

    async def async_listener(event):
        seen.append(("async", event.name))

    stream.on("tool_error", async_listener)
    asyncio.run(stream.run(graph, INPUT))
    assert seen == [("sync", "ats-reviewer"), ("async", "execute")]


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError, match="unknown event kind"):
        EventStream().on("on_everything", print)


def test_classify_ignores_internal_chains():
    raw = {
        "event": "on_chain_start",
        "name": "RunnableSequence",
        "run_id": "1",
        "parent_ids": ["0"],
        "metadata": {"lc_agent_name": "jobhunter"},
        "data": {},
    }
    assert EventStream().classify(raw) is None


def test_tool_error_detected_from_tool_message():
    stream = EventStream()
    raw = {
        "event": "on_tool_end",
        "name": "task",
        "run_id": "t",
        "parent_ids": ["0"],
        "metadata": {},
        "data": {
            "output": ToolMessage(content="Error: plan first", tool_call_id="x", status="error")
        },
    }
    assert stream.classify(raw).kind == "subagent_end"
    raw["name"] = "execute"
    assert stream.classify(raw).kind == "tool_error"
    raw["data"]["output"] = types.SimpleNamespace(
        update={"messages": [ToolMessage(content="ok", tool_call_id="x")]}
    )
    assert stream.classify(raw).kind == "tool_end"


def test_every_kind_has_a_renderer_listener():
    stream = EventStream()
    RichRenderer(Console(record=True)).attach(stream)
    assert all(stream._listeners[k] for k in KINDS)


# -- Rich rendering ---------------------------------------------------------------------------


def render(show_nodes: bool) -> str:
    graph, _ = scripted_graph()
    console = Console(record=True, width=110, force_terminal=False)
    stream = EventStream()
    renderer = RichRenderer(console, show_nodes=show_nodes).attach(stream)
    asyncio.run(stream.run(graph, INPUT))
    renderer.summary()
    return console.export_text()


def test_rendered_output():
    text = render(show_nodes=False)
    assert "[jobhunter] task → ats-reviewer" in text
    assert "Score the resume (score-only)." in text
    assert (
        "[ats-reviewer #1 › ats-reviewer] ▶ started" in text
        and "[ats-reviewer #1 › ats-reviewer] ■ finished" in text
    )
    assert "plan 0/1" in text and "plan 1/1" in text
    assert "write_file /output/ats/score.py" in text and "print(72.5)" in text
    assert "✓ write_file" in text
    assert "✗ execute" in text and "Execution not available" in text
    assert "ats-reviewer → result" in text
    # final answer streamed and rendered as Markdown (no raw ** / * markers)
    assert "Your resume scores 72.5. Biggest gain: add Kafka experience." in text
    assert "**72.5**" not in text
    assert "run summary" in text and "task→ats-reviewer" in text
    assert "input tokens (cached)" in text and "75%" in text
    assert "· model" not in text  # nodes hidden by default


def test_verbose_shows_nodes_and_usage():
    text = render(show_nodes=True)
    assert "· model" in text and "· tools" in text
    assert "tokens in 2,048 (cached 1,536) out 12" in text


def test_cli_turn_uses_the_event_stream():
    graph, _ = scripted_graph()
    console = Console(record=True, width=110)
    asyncio.run(_run_turn(graph, "score my resume", "t1", console=console))
    text = console.export_text()
    assert "task → ats-reviewer" in text and "run summary" in text


# -- parallel lanes ---------------------------------------------------------------------------

from genai_agentic_sandbox.streaming import lane_label


@pytest.mark.parametrize(
    ("label", "badge"),
    [
        ("job 1: acme-backend", "job 1"),
        ("job alpha: tailor", "job alpha"),
        ("ats: acme-backend", "ats: acme-back"),
        ("job-optimizer #3", "job-optimizer "),
    ],
)
def test_short_lane(label, badge):
    assert short_lane(label) == badge


@pytest.mark.parametrize(
    ("brief", "expected"),
    [
        ("[job 1: acme-backend] Tailor the resume...", "job 1: acme-backend"),
        ("Tailor for folder /output/resume/2-globex-platform/ please", "job 2: globex-platform"),
        ('mode "score-only", folder /output/ats/initech-ml/', "ats: initech-ml"),
        ("job beta, round 1", "job beta"),
        ("find 25 jobs in Kochi", "job-search #4"),
        (None, "task #1"),
    ],
)
def test_lane_label(brief, expected):
    subagent = "job-search" if brief == "find 25 jobs in Kochi" else None
    n = 4 if subagent else 1
    assert lane_label(brief, subagent, n) == expected


JOBS = {
    "alpha": "job 1: acme-backend",
    "beta": "job 2: globex-platform",
    "gamma": "job 3: initech-ml",
}


class RoutedModel(BaseChatModel):
    """One script per (agent, job): concurrent subagents must not share steps."""

    scripts: dict = Field(default_factory=dict)

    @property
    def _llm_type(self) -> str:
        return "routed"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        import threading
        import time

        system = str(messages[0].content)
        brief = next((str(m.content) for m in messages if m.type == "human"), "")
        role = (
            "orch"
            if "You are JobHunter" in system
            else "opt"
            if "job optimizer" in system
            else "build"
        )
        job = "" if role == "orch" else next(j for j in JOBS if f"job {j}" in brief)
        time.sleep(0.05)  # let the lanes interleave
        with self.__dict__.setdefault("_lock", threading.Lock()):
            message = self.scripts[(role, job)].pop(0)
        return ChatResult(generations=[ChatGeneration(message=message)])


def parallel_graph():
    def tc(prefix, name, args, n):
        return AIMessage(
            content="", tool_calls=[{"name": name, "args": args, "id": f"{prefix}{n}"}]
        )

    scripts = {
        ("orch", ""): [
            tc(
                "o",
                "write_todos",
                {
                    "todos": [
                        {"content": f"[{lane}] job-optimizer", "status": "in_progress"}
                        for lane in JOBS.values()
                    ]
                },
                1,
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "id": f"t-{j}",
                        "args": {
                            "subagent_type": "job-optimizer",
                            "description": f"[{lane}] job {j}: tailor",
                        },
                    }
                    for j, lane in JOBS.items()
                ],
            ),
            tc(
                "o",
                "write_todos",
                {
                    "todos": [
                        {"content": f"[{lane}] job-optimizer", "status": "completed"}
                        for lane in JOBS.values()
                    ]
                },
                2,
            ),
            AIMessage(content="All 3 jobs tailored."),
        ]
    }
    for j in JOBS:
        scripts[("opt", j)] = [
            tc(
                f"p{j}",
                "write_todos",
                {"todos": [{"content": "1. build", "status": "in_progress"}]},
                1,
            ),
            tc(
                f"p{j}",
                "task",
                {"subagent_type": "resume-builder", "description": f"job {j}, round 1"},
                2,
            ),
            tc(
                f"p{j}",
                "write_todos",
                {"todos": [{"content": "1. build", "status": "completed"}]},
                3,
            ),
            AIMessage(content=f"job {j}: best round 1"),
        ]
        scripts[("build", j)] = [
            tc(
                f"b{j}",
                "write_todos",
                {"todos": [{"content": "1. build", "status": "in_progress"}]},
                1,
            ),
            tc(f"b{j}", "ls", {"path": "/"}, 2),
            tc(
                f"b{j}",
                "write_todos",
                {"todos": [{"content": "1. build", "status": "completed"}]},
                3,
            ),
            AIMessage(content=f"built {j}"),
        ]
    model = RoutedModel(scripts=scripts)
    graph = create_jobhunter_agent(
        model=model,
        backend=StateBackend(),
        browser_tools=[],
        resume_path="/input/cv.docx",
        memory=False,
    )
    return graph, model


PARALLEL_INPUT = {"messages": [{"role": "user", "content": "tailor for 3 jobs"}]}
CONFIG = {"recursion_limit": 100}


@pytest.fixture(scope="module")
def lane_events():
    graph, model = parallel_graph()

    async def go():
        return [e async for e in EventStream().events(graph, PARALLEL_INPUT, CONFIG)]

    events = asyncio.run(go())
    assert all(not s for s in model.scripts.values())
    return events


def test_every_event_inside_a_lane_is_labelled(lane_events):
    for event in lane_events:
        if event.agent in ("job-optimizer", "resume-builder"):
            assert event.lane in JOBS.values(), event
        if (
            event.agent == "jobhunter"
            and event.kind != "subagent_start"
            and event.kind != "subagent_end"
        ):
            assert event.lane is None, event


def test_nested_events_keep_their_lane_and_path(lane_events):
    builder_tools = [
        e for e in lane_events if e.kind == "tool_start" and e.agent == "resume-builder"
    ]
    assert {e.lane for e in builder_tools} == set(JOBS.values())
    assert all(
        e.path == ("job-optimizer", "resume-builder") and e.depth == 2 for e in builder_tools
    )


def test_lanes_overlap_and_are_counted(lane_events):
    starts = [
        e for e in lane_events if e.kind == "subagent_start" and e.data.get("running") is not None
    ]
    assert [e.data["running"] for e in starts] == [1, 2, 3]
    assert [e.lane for e in starts] == list(JOBS.values())
    ends = [
        e
        for e in lane_events
        if e.kind == "subagent_end" and e.data.get("still_running") is not None
    ]
    assert sorted(e.data["still_running"] for e in ends) == [0, 1, 2]
    first_end = lane_events.index(ends[0])
    assert all(
        lane_events.index(s) < first_end for s in starts
    )  # all 3 started before any finished
    nested = [
        e for e in lane_events if e.kind == "subagent_start" and e.data.get("running") is None
    ]
    assert len(nested) == 3 and {e.lane for e in nested} == set(JOBS.values())


def test_parallel_rendering():
    graph, _ = parallel_graph()
    console = Console(record=True, width=140)
    stream = EventStream()
    renderer = RichRenderer(console).attach(stream)
    asyncio.run(stream.run(graph, PARALLEL_INPUT, CONFIG))
    renderer.summary()
    text = console.export_text()
    assert "⇉ 3 tasks launched in parallel" in text
    assert "3 running in parallel" in text and "still running" in text
    for lane in JOBS.values():
        assert f"[{lane} › resume-builder] 🔧 ls /" in text
        assert f"■ {lane}  job-optimizer finished in" in text
    # A tool call's argument box is titled with its job: no anonymous boxes.
    tool_boxes = [line for line in text.splitlines() if "🔧" in line]
    assert len(tool_boxes) == 3 and all(line.startswith("╭─") for line in tool_boxes)
    assert all(any(f"[{lane} ›" in line for lane in JOBS.values()) for line in tool_boxes)
    assert "parallel batch: 3 tasks" in text
    assert "lane job 1: acme-backend" in text  # per-lane line in the run summary
    # Start and end of every job: clock times on the lines, in the batch table and
    # the run summary, and a timeline bar per job on the batch's time axis.
    clock = r"\d\d:\d\d:\d\d"
    for lane in JOBS.values():
        st = renderer.lanes[lane]
        assert st["started_at"] < st["ended_at"]
        assert re.search(rf"▶ {lane}  job-optimizer started at {clock}", text)
        assert re.search(rf"■ {lane}  job-optimizer finished in [\d.]+s at {clock}", text)
        row = next(line for line in text.splitlines() if line.startswith(f"│ {lane} "))
        assert len(re.findall(clock, row)) == 2 and "█" in row, row
    assert re.search(rf"timeline: {clock} → {clock}", text)
    assert re.search(rf"lane job 1: acme-backend .*\({clock} → {clock}\)", text)
    assert all(renderer.lanes[lane]["status"] == "done" for lane in JOBS.values())


def test_parallel_lanes_stream_in_their_own_windows():
    graph, _ = parallel_graph()
    console = Console(record=True, width=180, height=60)
    stream = EventStream()
    renderer = RichRenderer(console, windows=True).attach(stream)
    asyncio.run(stream.run(graph, PARALLEL_INPUT, CONFIG))
    renderer.summary()
    text = console.export_text()
    assert not renderer.windows.active  # closed when the last lane finished
    # Lane lines go to the lane's window, not interleaved into the scrollback.
    assert "› resume-builder] 🔧" not in text
    for lane in JOBS.values():
        window = renderer.windows.windows[lane]
        assert window.status == "done" and window.subagent == "job-optimizer"
        rows = [line.plain for line in window.lines]
        badge = lane.split(":")[0]  # "job 1"
        # Every row after the brief is badged with its job, tool calls included.
        assert all(r.startswith(f"{badge} › ") for r in rows[1:]), rows
        assert f"{badge} ›   resume-builder 🔧 ls /" in rows, rows
        assert f"{badge} › job-optimizer task → resume-builder  job" in "\n".join(rows), rows
        assert "job-optimizer" in window.plans
        assert f" {lane} job-optimizer" in text  # the window's title in the final frame
        assert f"■ {lane}  job-optimizer finished in" in text
    assert "✔ done" in text
    assert "parallel batch: 3 tasks" in text


def test_windows_default_to_interactive_terminals_only():
    assert not RichRenderer(Console(record=True, force_terminal=False)).use_windows
    assert RichRenderer(Console(force_terminal=True)).use_windows


def test_lanes_survive_truncated_parent_ids():
    """With LangSmith tracing on, v2 events carry only their immediate parent id;
    the lane must then come from the checkpoint namespace (the bug: every lane
    showed 0 model / 0 tool calls)."""
    graph, _ = parallel_graph()

    async def raw_events():
        return [raw async for raw in graph.astream_events(PARALLEL_INPUT, CONFIG, version="v2")]

    raws = asyncio.run(raw_events())
    full = [
        (e.kind, e.agent, e.lane, e.path, e.depth) for e in map(EventStream().classify, raws) if e
    ]
    cut = EventStream()
    truncated = [
        cut.classify({**raw, "parent_ids": (raw.get("parent_ids") or [])[-1:]}) for raw in raws
    ]
    assert [(e.kind, e.agent, e.lane, e.path, e.depth) for e in truncated if e] == full

    # ...and the renderer counts every lane's calls from them.
    renderer = RichRenderer(Console(record=True, width=140))
    stream = EventStream()
    renderer.attach(stream)

    async def replay():
        for raw in raws:
            if event := stream.classify({**raw, "parent_ids": (raw.get("parent_ids") or [])[-1:]}):
                await stream.dispatch(event)

    asyncio.run(replay())
    for lane in JOBS.values():
        assert renderer.lanes[lane]["model"] == 8 and renderer.lanes[lane]["tools"] == 1
