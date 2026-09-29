"""TodoCompletionMiddleware: plan with todos first, finish every todo before answering."""

from __future__ import annotations

import asyncio

import pytest
from genai_agentic_sandbox.middleware import (
    DEFAULT_MAX_REMINDERS,
    REMINDER_PREFIX,
    TodoCompletionMiddleware,
    open_todos,
    reminder_text,
)
from langchain.agents import create_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import Field

DONE = {"content": "a", "status": "completed"}
PENDING = {"content": "b", "status": "pending"}
ACTIVE = {"content": "c", "status": "in_progress"}


class ScriptedModel(BaseChatModel):
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


def call(name, args, n):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"t{n}"}])


def todos_call(todos, n):
    return call("write_todos", {"todos": todos}, n)


def state(*todos, last=None, reminders=0):
    return {
        "messages": [last or AIMessage(content="final answer")],
        "todos": list(todos),
        "todo_reminders": reminders,
    }


class Req:
    def __init__(self, name, state):
        self.tool_call = {"name": name, "args": {}, "id": "c1"}
        self.state = state


def ok(request):
    return ToolMessage(content="done", tool_call_id="c1")


# -- finish only when every todo is completed ---------------------------------------------


def test_open_todos():
    assert open_todos({"todos": [DONE, PENDING, ACTIVE]}) == [PENDING, ACTIVE]
    assert open_todos({}) == []


def test_finishing_with_open_todos_goes_back_to_the_model():
    update = TodoCompletionMiddleware().after_model(state(DONE, PENDING, ACTIVE), None)
    assert update["jump_to"] == "model" and update["todo_reminders"] == 1
    [reminder] = update["messages"]
    assert isinstance(reminder, HumanMessage) and reminder.content.startswith(REMINDER_PREFIX)
    assert "may not finish yet" in reminder.content and "2 open item(s)" in reminder.content
    assert "- [pending] b" in reminder.content and "- [in_progress] c" in reminder.content


@pytest.mark.parametrize(
    "s",
    [
        state(DONE),
        state(),
        state(
            PENDING, last=AIMessage(content="", tool_calls=[{"name": "ls", "args": {}, "id": "1"}])
        ),
    ],
)
def test_no_reminder_needed(s):
    assert TodoCompletionMiddleware().after_model(s, None) is None


def test_reminders_continue_until_the_safety_cap():
    mw = TodoCompletionMiddleware()
    assert DEFAULT_MAX_REMINDERS == 10
    for sent in range(DEFAULT_MAX_REMINDERS):
        assert mw.after_model(state(PENDING, reminders=sent), None) is not None
    assert mw.after_model(state(PENDING, reminders=DEFAULT_MAX_REMINDERS), None) is None


def test_async_hook_matches_sync():
    mw = TodoCompletionMiddleware()
    assert asyncio.run(mw.aafter_model(state(PENDING), None)) == mw.after_model(
        state(PENDING), None
    )


def test_reminder_text_lists_items():
    assert "write_todos" in reminder_text([PENDING])


# -- plan before working ----------------------------------------------------------------


def test_work_tools_refused_before_the_plan():
    ran = []
    mw = TodoCompletionMiddleware(plan_before=("task", "execute"))
    for name in ("task", "execute"):
        result = mw.wrap_tool_call(Req(name, {}), lambda r: ran.append(r))
        assert (
            result.status == "error" and "plan first" in result.content and name in result.content
        )
    assert ran == []


def test_reading_is_allowed_before_the_plan():
    mw = TodoCompletionMiddleware(plan_before=("task",))
    assert mw.wrap_tool_call(Req("read_file", {}), ok).content == "done"


def test_work_tools_allowed_once_planned():
    mw = TodoCompletionMiddleware(plan_before=("task",))
    assert mw.wrap_tool_call(Req("task", {"todos_planned": True}), ok).content == "done"


def test_write_todos_marks_the_request_planned():
    mw = TodoCompletionMiddleware(plan_before=("task",))
    result = mw.wrap_tool_call(
        Req("write_todos", {}), lambda r: Command(update={"todos": [PENDING]})
    )
    assert result.update == {"todos": [PENDING], "todos_planned": True}


def test_each_request_starts_unplanned_with_fresh_reminders():
    assert TodoCompletionMiddleware().before_agent(
        {"todos_planned": True, "todo_reminders": 7}, None
    ) == {
        "todo_reminders": 0,
        "todos_planned": False,
    }


# -- in a real agent graph ------------------------------------------------------------------


def make_agent(script, **mw_kwargs):
    ran = []

    @tool
    def execute(command: str) -> str:
        """Run a command."""
        ran.append(command)
        return "ran"

    model = ScriptedModel(script=script)
    agent = create_agent(
        model,
        tools=[execute],
        middleware=[
            TodoListMiddleware(),
            TodoCompletionMiddleware(plan_before=("execute",), **mw_kwargs),
        ],
        checkpointer=InMemorySaver(),
    )
    return model, agent, ran


def test_agent_plans_works_and_completes_every_todo():
    plan = [
        {"content": "1. run", "status": "in_progress"},
        {"content": "2. check", "status": "pending"},
    ]
    model, agent, ran = make_agent(
        [
            call("execute", {"command": "too early"}, 1),  # refused: no plan yet
            todos_call(plan, 2),
            call("execute", {"command": "python job.py"}, 3),
            todos_call([dict(plan[0], status="completed"), dict(plan[1], status="in_progress")], 4),
            AIMessage(content="done (too early)"),  # step 2 still open -> sent back
            todos_call([dict(t, status="completed") for t in plan], 5),
            AIMessage(content="all steps done"),
        ]
    )
    out = agent.invoke(
        {"messages": [{"role": "user", "content": "go"}]}, {"configurable": {"thread_id": "1"}}
    )
    assert model.script == []
    assert ran == ["python job.py"]
    assert out["messages"][-1].content == "all steps done"
    assert all(t["status"] == "completed" for t in out["todos"])
    refusal = next(m for m in out["messages"] if isinstance(m, ToolMessage) and m.status == "error")
    assert "plan first" in refusal.content
    reminders = [
        m
        for m in out["messages"]
        if isinstance(m, HumanMessage) and m.content.startswith(REMINDER_PREFIX)
    ]
    assert len(reminders) == 1 and "2. check" in reminders[0].content


def test_new_request_needs_a_new_plan():
    """Same thread, second request: the old (completed) todo list does not unlock work."""
    model, agent, ran = make_agent(
        [
            todos_call([{"content": "1. run", "status": "completed"}], 1),
            AIMessage(content="first done"),
            call("execute", {"command": "second request, no plan"}, 2),  # refused
            todos_call([{"content": "1. again", "status": "in_progress"}], 3),
            call("execute", {"command": "planned now"}, 4),
            todos_call([{"content": "1. again", "status": "completed"}], 5),
            AIMessage(content="second done"),
        ]
    )
    cfg = {"configurable": {"thread_id": "t"}}
    agent.invoke({"messages": [{"role": "user", "content": "first"}]}, cfg)
    out = agent.invoke({"messages": [{"role": "user", "content": "second"}]}, cfg)
    assert model.script == [] and ran == ["planned now"]
    assert out["messages"][-1].content == "second done"


def test_blocked_agent_stops_at_the_safety_cap():
    model, agent, _ = make_agent(
        [todos_call([PENDING], 1), AIMessage(content="blocked 1"), AIMessage(content="blocked 2")],
        max_reminders=1,
    )
    out = agent.invoke(
        {"messages": [{"role": "user", "content": "go"}]}, {"configurable": {"thread_id": "1"}}
    )
    assert out["messages"][-1].content == "blocked 2" and model.script == []


def test_works_when_run_async():
    model, agent, _ = make_agent(
        [
            todos_call([PENDING], 1),
            AIMessage(content="early"),
            todos_call([dict(PENDING, status="completed")], 2),
            AIMessage(content="done"),
        ]
    )

    async def go():
        return await agent.ainvoke(
            {"messages": [{"role": "user", "content": "go"}]}, {"configurable": {"thread_id": "1"}}
        )

    assert asyncio.run(go())["messages"][-1].content == "done" and model.script == []


def test_user_input_revises_the_plan_through_write_todos():
    """Second request in the same thread ("also Bangalore"): the agent must revise its
    todo list before working again; completed items stay, new ones are added."""
    first = [{"content": "1. search Kochi", "status": "completed"}]
    revised = [*first, {"content": "2. search Bangalore (user asked)", "status": "in_progress"}]
    model, agent, ran = make_agent(
        [
            todos_call([dict(first[0], status="in_progress")], 1),
            call("execute", {"command": "search kochi"}, 2),
            todos_call(first, 3),
            AIMessage(content="Kochi done"),
            todos_call(revised, 4),  # user input -> revised plan
            call("execute", {"command": "search bangalore"}, 5),
            todos_call([dict(t, status="completed") for t in revised], 6),
            AIMessage(content="Bangalore added"),
        ]
    )
    cfg = {"configurable": {"thread_id": "u"}}
    agent.invoke({"messages": [{"role": "user", "content": "find jobs in Kochi"}]}, cfg)
    out = agent.invoke({"messages": [{"role": "user", "content": "also Bangalore"}]}, cfg)
    assert model.script == [] and ran == ["search kochi", "search bangalore"]
    assert [t["content"] for t in out["todos"]] == [
        "1. search Kochi",
        "2. search Bangalore (user asked)",
    ]
    assert all(t["status"] == "completed" for t in out["todos"])
