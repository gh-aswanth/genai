"""Event stream v2 -> typed events -> listeners -> Rich rendering."""

from __future__ import annotations

import asyncio
import types

import pytest
from deepagents.backends import StateBackend
from genai_agentic_sandbox.agent import create_jobhunter_agent
from genai_agentic_sandbox.main import _run_turn
from genai_agentic_sandbox.streaming import KINDS, AgentEvent, EventStream, RichRenderer
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
    assert "[ats-reviewer] ▶ started" in text and "[ats-reviewer] ■ finished" in text
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
