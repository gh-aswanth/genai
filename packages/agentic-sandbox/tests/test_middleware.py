"""TodoCompletionMiddleware: agents may not finish with open todos."""

from __future__ import annotations

import asyncio

import pytest
from genai_agentic_sandbox.middleware import (
    REMINDER_PREFIX,
    TodoCompletionMiddleware,
    open_todos,
    reminder_text,
)
from langchain.agents import create_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
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


def todos_call(todos, n):
    return AIMessage(
        content="", tool_calls=[{"name": "write_todos", "args": {"todos": todos}, "id": f"t{n}"}]
    )


def state(*todos, last=None, reminders=0):
    return {
        "messages": [last or AIMessage(content="final answer")],
        "todos": list(todos),
        "todo_reminders": reminders,
    }


# -- the hook ------------------------------------------------------------------------


def test_open_todos():
    assert open_todos({"todos": [DONE, PENDING, ACTIVE]}) == [PENDING, ACTIVE]
    assert open_todos({}) == []


def test_finishing_with_open_todos_goes_back_to_the_model():
    update = TodoCompletionMiddleware().after_model(state(DONE, PENDING, ACTIVE), None)
    assert update["jump_to"] == "model"
    assert update["todo_reminders"] == 1
    [reminder] = update["messages"]
    assert isinstance(reminder, HumanMessage)
    assert reminder.content.startswith(REMINDER_PREFIX)
    assert "2 open item(s)" in reminder.content
    assert "- [pending] b" in reminder.content and "- [in_progress] c" in reminder.content


@pytest.mark.parametrize(
    "s",
    [
        state(DONE),  # everything completed
        state(),  # no plan at all (simple request)
        state(
            PENDING, last=AIMessage(content="", tool_calls=[{"name": "ls", "args": {}, "id": "1"}])
        ),  # still working
    ],
)
def test_no_reminder_needed(s):
    assert TodoCompletionMiddleware().after_model(s, None) is None


def test_reminders_are_capped():
    mw = TodoCompletionMiddleware(max_reminders=2)
    assert mw.after_model(state(PENDING, reminders=1), None) is not None
    assert mw.after_model(state(PENDING, reminders=2), None) is None


def test_async_hook_matches_sync():
    mw = TodoCompletionMiddleware()
    assert asyncio.run(mw.aafter_model(state(PENDING), None)) == mw.after_model(
        state(PENDING), None
    )


def test_reminder_text_lists_items():
    assert "write_todos" in reminder_text([PENDING])


# -- in a real agent graph -------------------------------------------------------------


def run_agent(script, **mw_kwargs):
    model = ScriptedModel(script=script)
    agent = create_agent(
        model, tools=[], middleware=[TodoListMiddleware(), TodoCompletionMiddleware(**mw_kwargs)]
    )
    return model, agent


def test_agent_is_sent_back_and_finishes_its_plan():
    plan = [
        {"content": "step 1", "status": "completed"},
        {"content": "step 2", "status": "pending"},
    ]
    model, agent = run_agent(
        [
            todos_call(plan, 1),
            AIMessage(content="done (too early)"),
            todos_call([dict(t, status="completed") for t in plan], 2),
            AIMessage(content="all steps done"),
        ]
    )
    out = agent.invoke({"messages": [{"role": "user", "content": "go"}]})
    assert model.script == []
    assert out["messages"][-1].content == "all steps done"
    assert all(t["status"] == "completed" for t in out["todos"])
    reminders = [
        m
        for m in out["messages"]
        if isinstance(m, HumanMessage) and m.content.startswith(REMINDER_PREFIX)
    ]
    assert len(reminders) == 1 and "step 2" in reminders[0].content


def test_blocked_agent_stops_after_the_cap():
    model, agent = run_agent(
        [todos_call([PENDING], 1), AIMessage(content="blocked 1"), AIMessage(content="blocked 2")],
        max_reminders=1,
    )
    out = agent.invoke({"messages": [{"role": "user", "content": "go"}]})
    assert out["messages"][-1].content == "blocked 2"
    assert model.script == []


def test_works_when_streamed_async():
    model, agent = run_agent(
        [
            todos_call([PENDING], 1),
            AIMessage(content="early"),
            todos_call([dict(PENDING, status="completed")], 2),
            AIMessage(content="done"),
        ]
    )

    async def go():
        return await agent.ainvoke({"messages": [{"role": "user", "content": "go"}]})

    assert asyncio.run(go())["messages"][-1].content == "done"
    assert model.script == []
