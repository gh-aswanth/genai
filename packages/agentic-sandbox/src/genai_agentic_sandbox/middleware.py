"""Todo discipline: plan with `write_todos` first, finish every todo before answering.

`TodoListMiddleware` gives an agent `write_todos`; `TodoCompletionMiddleware`
makes it use it properly, for every request:

1. **Plan first.** The tools that do the real work (`plan_before`, e.g. `task`
   for the orchestrator, `execute` / `write_file` / `edit_file` for subagents)
   are refused until the agent has written its todo list in this request.
   Reading and inspecting are always allowed, so the agent can look before it
   plans.
2. **Finish before answering.** When the agent tries to finish (a model turn
   with no tool calls) while any todo is still `pending` or `in_progress`, it is
   sent back with the list of open items. The only ways out are completing them
   or explicitly closing them with `write_todos` (mark completed / remove, and
   say why). A generous safety cap (`max_reminders`) stops a truly stuck agent
   from looping until the recursion limit.

Both are reset at the start of each request (`before_agent`), so a completed
list from an earlier request neither counts as this request's plan nor uses up
this request's reminders.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain.agents.middleware.todo import Todo
from langchain.agents.middleware.types import OmitFromSchema
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

logger = logging.getLogger(__name__)

REMINDER_PREFIX = "[todo check]"
DEFAULT_MAX_REMINDERS = 10


class TodoCompletionState(AgentState):
    todos: Annotated[NotRequired[list[Todo]], OmitFromSchema(input=True, output=False)]
    todo_reminders: Annotated[NotRequired[int], OmitFromSchema(input=True, output=True)]
    todos_planned: Annotated[NotRequired[bool], OmitFromSchema(input=True, output=True)]


def open_todos(state: dict[str, Any]) -> list[Todo]:
    return [t for t in state.get("todos") or [] if t.get("status") != "completed"]


def reminder_text(todos: list[Todo]) -> str:
    items = "\n".join(f"- [{t.get('status', 'pending')}] {t.get('content', '')}" for t in todos)
    return (
        f"{REMINDER_PREFIX} You may not finish yet: your todo list still has "
        f"{len(todos)} open item(s):\n{items}\n"
        "Continue with the next open item now. Only when an item is truly done, no "
        "longer needed, or blocked, call `write_todos` to mark it completed or remove it "
        "- and say why in your answer. Finish only when every item is completed."
    )


def plan_first_text(tool: str) -> str:
    return (
        f"Error: plan first. Call `write_todos` (on its own, before `{tool}`) with one item "
        "per step of your plan, the first one in_progress; then do the steps in order."
    )


def _ok(result: Any) -> bool:
    messages = result.update.get("messages", []) if isinstance(result, Command) else [result]
    return not any(
        isinstance(m, ToolMessage) and (m.status == "error" or str(m.content).startswith("Error"))
        for m in messages
    )


def _with_state(result: Any, update: dict) -> Command:
    if isinstance(result, Command):
        return Command(update={**(result.update or {}), **update})
    return Command(update={"messages": [result], **update})


class TodoCompletionMiddleware(AgentMiddleware):
    """Plan with todos before working; complete every todo before answering.

    Args:
        plan_before: Tool names refused until `write_todos` was called in this request.
        max_reminders: Safety cap on "you still have open todos" returns per request.
    """

    state_schema = TodoCompletionState

    def __init__(
        self, plan_before: tuple[str, ...] = (), max_reminders: int = DEFAULT_MAX_REMINDERS
    ) -> None:
        super().__init__()
        self.plan_before = tuple(plan_before)
        self.max_reminders = max_reminders

    # -- per request -------------------------------------------------------------------

    def before_agent(self, state: TodoCompletionState, runtime: Any) -> dict[str, Any] | None:
        return {"todo_reminders": 0, "todos_planned": False}

    async def abefore_agent(
        self, state: TodoCompletionState, runtime: Any
    ) -> dict[str, Any] | None:
        return self.before_agent(state, runtime)

    # -- finish only when everything is completed ---------------------------------------

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
            logger.warning(
                "agent finished with %d open todos after %d reminders", len(remaining), sent
            )
            return None
        return {
            "messages": [HumanMessage(content=reminder_text(remaining))],
            "todo_reminders": sent + 1,
            "jump_to": "model",
        }

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: TodoCompletionState, runtime: Any) -> dict[str, Any] | None:
        return self.after_model(state, runtime)

    # -- plan before working -------------------------------------------------------------

    def _refuse(self, request: Any) -> ToolMessage | None:
        name = request.tool_call.get("name")
        state = request.state if isinstance(request.state, dict) else {}
        if name in self.plan_before and not state.get("todos_planned"):
            return ToolMessage(
                content=plan_first_text(name), tool_call_id=request.tool_call["id"], status="error"
            )
        return None

    def _after(self, request: Any, result: Any) -> Any:
        if request.tool_call.get("name") == "write_todos" and _ok(result):
            return _with_state(result, {"todos_planned": True})
        return result

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        refused = self._refuse(request)
        return refused if refused is not None else self._after(request, handler(request))

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        refused = self._refuse(request)
        return refused if refused is not None else self._after(request, await handler(request))


__all__ = [
    "DEFAULT_MAX_REMINDERS",
    "REMINDER_PREFIX",
    "TodoCompletionMiddleware",
    "open_todos",
    "plan_first_text",
    "reminder_text",
]
