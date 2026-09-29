"""Middleware that makes agents finish what they planned.

`TodoListMiddleware` gives an agent `write_todos`, but nothing stops it from
answering while items are still pending. `TodoCompletionMiddleware` checks at
the moment the agent tries to finish (a model turn with no tool calls): if the
todo list still has open items it sends the agent back to the model with a
reminder listing them, so it either does the work or explicitly closes the items
(marks them completed or removes them, saying why). A small cap on reminders
keeps a stuck agent from looping forever.
"""

from __future__ import annotations

from typing import Annotated, Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain.agents.middleware.todo import Todo
from langchain.agents.middleware.types import OmitFromSchema
from langchain_core.messages import AIMessage, HumanMessage

REMINDER_PREFIX = "[todo check]"


class TodoCompletionState(AgentState):
    todos: Annotated[NotRequired[list[Todo]], OmitFromSchema(input=True, output=False)]
    todo_reminders: Annotated[NotRequired[int], OmitFromSchema(input=True, output=True)]


def open_todos(state: dict[str, Any]) -> list[Todo]:
    return [t for t in state.get("todos") or [] if t.get("status") != "completed"]


def reminder_text(todos: list[Todo]) -> str:
    items = "\n".join(f"- [{t.get('status', 'pending')}] {t.get('content', '')}" for t in todos)
    return (
        f"{REMINDER_PREFIX} You are about to finish, but your todo list still has "
        f"{len(todos)} open item(s):\n{items}\n"
        "Do the remaining work now. If an item is already done, no longer needed or "
        "blocked, call `write_todos` to mark it completed or remove it (say why in your "
        "answer). Then give your final answer."
    )


class TodoCompletionMiddleware(AgentMiddleware):
    """Send an agent back to work when it tries to finish with open todos.

    Args:
        max_reminders: How many times per run the agent is sent back before it is
            allowed to finish anyway (so a blocked agent cannot loop forever).
    """

    state_schema = TodoCompletionState

    def __init__(self, max_reminders: int = 2) -> None:
        super().__init__()
        self.max_reminders = max_reminders

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: TodoCompletionState, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        if not messages or not isinstance(messages[-1], AIMessage) or messages[-1].tool_calls:
            return None
        remaining = open_todos(state)
        if not remaining:
            return None
        sent = state.get("todo_reminders", 0)
        if sent >= self.max_reminders:
            return None
        return {
            "messages": [HumanMessage(content=reminder_text(remaining))],
            "todo_reminders": sent + 1,
            "jump_to": "model",
        }

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: TodoCompletionState, runtime: Any) -> dict[str, Any] | None:
        return self.after_model(state, runtime)


__all__ = ["REMINDER_PREFIX", "TodoCompletionMiddleware", "open_todos", "reminder_text"]
