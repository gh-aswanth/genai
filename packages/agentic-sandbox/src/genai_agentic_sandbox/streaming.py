"""Live view of a run: LangChain event stream v2 -> typed events -> listeners -> Rich.

`EventStream` consumes `graph.astream_events(..., version="v2")` and turns each
raw event into one typed `AgentEvent`, dispatched to the listeners registered
for its kind:

| kind | raw v2 event | what it is |
|---|---|---|
| `agent_start` / `agent_end` | `on_chain_start/end` of a graph run | the orchestrator (depth 0) or a subagent's graph (depth >= 1) |
| `subagent_start` / `subagent_end` | `on_tool_start/end` of `task` | the orchestrator delegating to a subagent and getting its answer |
| `node_start` / `node_end` | `on_chain_start/end` of `model`, `tools`, `*Middleware.*` | graph nodes (subgraph steps) |
| `model_start` / `model_token` / `model_end` | `on_chat_model_start/stream/end` | a model call, its streamed tokens, its result and usage |
| `tool_start` / `tool_end` / `tool_error` | `on_tool_start/end/error` (not `task` / `write_todos`) | tool calls |
| `todos` | `on_tool_start` of `write_todos` | the agent's plan (todo list) changed |
| `custom` | `on_custom_event` | anything a tool/middleware dispatches |

Which agent an event belongs to comes from `metadata["lc_agent_name"]`; its depth
from how many `task` runs it is inside - found from its parent run ids and, as
those are cut to the immediate parent when LangSmith tracing is on, from its
LangGraph checkpoint namespace. `RichRenderer` registers one
listener per kind and renders them with a Rich console.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

KINDS = (
    "agent_start",
    "agent_end",
    "subagent_start",
    "subagent_end",
    "node_start",
    "node_end",
    "model_start",
    "model_token",
    "model_end",
    "tool_start",
    "tool_end",
    "tool_error",
    "todos",
    "custom",
)
_NODE_NAMES = {"model", "tools"}


@dataclass
class AgentEvent:
    """One classified event."""

    kind: str
    agent: str
    depth: int
    name: str
    run_id: str
    data: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    # The parallel lane (the orchestrator's `task` this event belongs to) and the
    # chain of subagents inside it, outermost first. None / () for the orchestrator.
    lane: str | None = None
    path: tuple[str, ...] = ()


@dataclass
class TaskRun:
    """A running `task` (subagent call)."""

    label: str
    subagent: str
    lane: str  # label of the outermost task it runs in (itself, for a top-level task)
    started: float
    top_level: bool  # started by the orchestrator (one parallel lane)
    started_at: float = field(default_factory=time.time)  # wall clock, for display
    ns: tuple[str, ...] = ()  # checkpoint namespace the call ran in, e.g. ("tools:<id>",)


_LABEL = re.compile(r"^\s*\[([^\]\n]{1,48})\]")
_JOB_FOLDER = re.compile(r"/output/resume/(\d+)-([a-z0-9][a-z0-9-]*)")
_JOB_WORD = re.compile(r"\bjob\b[ :#]*([A-Za-z0-9][\w-]*)", re.IGNORECASE)
_ATS_FOLDER = re.compile(r"/output/ats/([a-z0-9][a-z0-9-]*)")


def lane_label(description: str | None, subagent: str | None, n: int) -> str:
    """A short, stable label for a task: the brief's own "[label]" prefix, else the
    job it works on (from its folder or "job <x>"), else "<subagent> #n"."""
    text = description or ""
    if m := _LABEL.search(text):
        return m.group(1).strip()
    if m := _JOB_FOLDER.search(text):
        return f"job {m.group(1)}: {m.group(2)}"
    if m := _ATS_FOLDER.search(text):
        return f"ats: {m.group(1)}"
    if m := _JOB_WORD.search(text):
        return f"job {m.group(1)}"
    return f"{subagent or 'task'} #{n}"


_SHORT_JOB = re.compile(r"^(job [\w-]+)\s*:")


def short_lane(label: str) -> str:
    """The lane's short badge: "job 1" for "job 1: acme-backend", else the label
    cut to 14 characters."""
    m = _SHORT_JOB.match(label)
    return m.group(1) if m else label[:14]


Listener = Callable[[AgentEvent], Awaitable[None] | None]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return "" if content is None else str(content)


def _tool_output(output: Any) -> tuple[str, bool]:
    """(text, is_error) of a tool result (ToolMessage, Command or plain value)."""
    update = getattr(output, "update", None)
    if isinstance(update, dict):
        messages = update.get("messages") or []
        output = messages[-1] if messages else ""
    status = getattr(output, "status", None)
    text = _text(getattr(output, "content", output))
    return text, status == "error" or text.startswith("Error")


class EventStream:
    """Classify v2 events and dispatch them to listeners per kind."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = defaultdict(list)
        self._task_runs: dict[str, TaskRun] = {}  # run_id of a `task` call -> its run
        self._task_count = 0

    def on(self, kind: str, listener: Listener | None = None):
        """Register a listener for `kind` (usable as a decorator)."""
        if kind not in KINDS:
            raise ValueError(f"unknown event kind {kind!r}; one of {KINDS}")

        def register(fn: Listener) -> Listener:
            self._listeners[kind].append(fn)
            return fn

        return register(listener) if listener is not None else register

    # -- classification ----------------------------------------------------------

    @staticmethod
    def _ns(raw: dict) -> tuple[str, ...]:
        ns = (raw.get("metadata") or {}).get("langgraph_checkpoint_ns") or ""
        return tuple(ns.split("|")) if ns else ()

    def _tasks(self, raw: dict) -> list[TaskRun]:
        """The running `task` calls this event is inside, outermost first.

        From the event's parent run ids and from its checkpoint namespace: all a
        `task` call runs is namespaced under the `tools:<id>` it ran in. The
        namespace is needed because the parent ids are not always complete - with
        LangSmith tracing on they hold only the immediate parent, which would leave
        every event inside a subagent unattributed (0 model / 0 tool calls per lane).
        """
        found = {p: self._task_runs[p] for p in raw.get("parent_ids") or [] if p in self._task_runs}
        if ns := self._ns(raw):
            for run_id, run in self._task_runs.items():
                if run.ns and ns[: len(run.ns)] == run.ns:
                    found[run_id] = run
        return sorted(found.values(), key=lambda run: run.started)  # outer ones start first

    def _depth(self, raw: dict) -> int:
        return len(self._tasks(raw))

    def _lane(self, raw: dict) -> tuple[str | None, tuple[str, ...]]:
        """(lane, subagent path) from the `task` runs the event is inside (root first)."""
        tasks = self._tasks(raw)
        return (tasks[0].lane if tasks else None), tuple(t.subagent for t in tasks)

    @property
    def running(self) -> dict[str, TaskRun]:
        """Tasks started and not finished yet, by run id."""
        return dict(self._task_runs)

    def lanes_running(self) -> int:
        """How many of the orchestrator's tasks (parallel lanes) are running now."""
        return sum(1 for t in self._task_runs.values() if t.top_level)

    def classify(self, raw: dict) -> AgentEvent | None:
        event, name = raw.get("event", ""), raw.get("name", "")
        metadata, data = raw.get("metadata") or {}, raw.get("data") or {}
        agent = metadata.get("lc_agent_name") or name or "agent"
        run_id = raw.get("run_id", "")

        def make(kind: str, **payload: Any) -> AgentEvent:
            lane, path = self._lane(raw)
            return AgentEvent(kind, agent, self._depth(raw), name, run_id, payload, raw, lane, path)

        if event in ("on_chain_start", "on_chain_end"):
            phase = "start" if event.endswith("start") else "end"
            parents = raw.get("parent_ids") or []
            if not parents or (name == metadata.get("lc_agent_name") and self._depth(raw) > 0):
                return make(f"agent_{phase}", output=data.get("output"))
            if name in _NODE_NAMES or "Middleware" in name:
                return make(f"node_{phase}")
            return None
        if event == "on_chat_model_start":
            messages = (data.get("input") or {}).get("messages") or [[]]
            return make(
                "model_start", messages=len(messages[0]), model=metadata.get("ls_model_name")
            )
        if event == "on_chat_model_stream":
            chunk = data.get("chunk")
            text = _text(getattr(chunk, "content", ""))
            return make("model_token", text=text) if text else None
        if event == "on_chat_model_end":
            message = data.get("output")
            return make(
                "model_end",
                text=_text(getattr(message, "content", "")),
                tool_calls=list(getattr(message, "tool_calls", None) or []),
                usage=dict(getattr(message, "usage_metadata", None) or {}),
            )
        if event == "on_tool_start":
            args = data.get("input") or {}
            if name == "task":
                subagent = args.get("subagent_type", "subagent")
                self._task_count += 1
                label = lane_label(args.get("description"), subagent, self._task_count)
                outer, _ = self._lane(raw)
                event = make(
                    "subagent_start", subagent=subagent, task=args.get("description"), label=label
                )
                self._task_runs[run_id] = TaskRun(
                    label,
                    subagent,
                    outer or label,
                    time.monotonic(),
                    top_level=outer is None,
                    ns=self._ns(raw),
                )
                if outer is None:
                    event.lane = label
                    event.data["running"] = self.lanes_running()
                    event.data["started_at"] = self._task_runs[run_id].started_at
                return event
            if name == "write_todos":
                return make("todos", todos=list(args.get("todos") or []))
            return make("tool_start", args=args)
        if event == "on_tool_end":
            text, error = _tool_output(data.get("output"))
            if name == "task":
                run = self._task_runs.pop(run_id, None)
                event = make(
                    "subagent_end",
                    subagent=run.subagent if run else None,
                    output=text,
                    label=run.label if run else None,
                    error=error,
                    seconds=time.monotonic() - run.started if run else None,
                )
                if run is not None and run.top_level:
                    event.lane = run.label
                    event.data["still_running"] = self.lanes_running()
                    event.data["started_at"], event.data["ended_at"] = run.started_at, time.time()
                return event
            if name == "write_todos":
                return None
            return make("tool_error" if error else "tool_end", output=text)
        if event == "on_tool_error":
            return make("tool_error", output=str(data.get("error", "")))
        if event == "on_custom_event":
            return make("custom", payload=data)
        return None

    # -- dispatch -----------------------------------------------------------------

    async def dispatch(self, event: AgentEvent) -> None:
        for listener in self._listeners.get(event.kind, []):
            result = listener(event)
            if hasattr(result, "__await__"):
                await result

    async def run(self, graph: Any, inputs: Any, config: dict | None = None) -> None:
        """Stream one invocation of `graph`, dispatching every classified event."""
        async for raw in graph.astream_events(inputs, config=config, version="v2"):
            event = self.classify(raw)
            if event is not None:
                await self.dispatch(event)

    async def events(
        self, graph: Any, inputs: Any, config: dict | None = None
    ) -> AsyncIterator[AgentEvent]:
        """Same stream as `run`, as an async iterator (no listeners involved)."""
        async for raw in graph.astream_events(inputs, config=config, version="v2"):
            event = self.classify(raw)
            if event is not None:
                yield event


# -- Rich rendering -----------------------------------------------------------------

AGENT_COLOURS = {
    "jobhunter": "bright_cyan",
    "job-search": "magenta",
    "job-matcher": "yellow",
    "job-optimizer": "bright_magenta",
    "resume-builder": "green",
    "ats-reviewer": "blue",
    "general-purpose": "white",
}
# One colour per parallel lane, in the order lanes appear.
LANE_COLOURS = (
    "dark_orange",
    "green3",
    "deep_sky_blue1",
    "orchid",
    "gold1",
    "spring_green2",
    "light_coral",
    "turquoise2",
)
_TODO = {"completed": ("✔", "green"), "in_progress": ("◐", "yellow"), "pending": ("○", "grey50")}
_CODE_EXT = {
    ".py": "python",
    ".json": "json",
    ".md": "markdown",
    ".sh": "bash",
    ".js": "javascript",
}


def _line(*parts: Any) -> Text:
    """One window row: never wraps, so a window's height is exactly its row count."""
    text = Text.assemble(*parts)
    text.no_wrap, text.overflow = True, "ellipsis"
    return text


def _clock(ts: float | None) -> str:
    """Wall-clock time of day, e.g. "14:02:11"; "-" when unknown."""
    return datetime.fromtimestamp(ts).astimezone().strftime("%H:%M:%S") if ts else "-"


def _first_line(text: Any, width: int = 200) -> str:
    lines = str(text or "").strip().splitlines()
    return lines[0][:width] if lines else ""


@dataclass
class LaneWindow:
    """What one parallel lane's window shows."""

    lane: str
    subagent: str
    started: float
    lines: deque[Text] = field(default_factory=lambda: deque(maxlen=300))
    plans: dict[str, list[dict]] = field(default_factory=dict)  # agent -> its latest todos
    streaming: str = ""  # text of the model call streaming in this lane right now
    status: str = "running"
    seconds: float | None = None


class LaneWindows:
    """A live grid with one window per parallel lane.

    Each window shows its lane's plans (one progress line per agent), a scrolling
    log of what its agents do (subagent calls, tools, answers) and, at the bottom,
    the model text streaming in that lane right now. The grid is redrawn in place
    while lanes run; everything outside the lanes still prints above it. When the
    last lane finishes, the final frame stays on screen.
    """

    MIN_WIDTH = 56  # narrowest window before the grid drops a column

    def __init__(self, renderer: RichRenderer) -> None:
        self.renderer = renderer
        self.windows: dict[str, LaneWindow] = {}
        self._live: Live | None = None

    @property
    def active(self) -> bool:
        return self._live is not None

    def get(self, event: AgentEvent) -> LaneWindow | None:
        return self.windows.get(event.lane) if self._live and event.lane else None

    def open(self, lane: str, subagent: str, brief: str) -> None:
        if self._live is None:
            self.windows = {}
            self._live = Live(
                self, console=self.renderer.console, refresh_per_second=8, transient=False
            )
            self._live.start()
        window = self.windows[lane] = LaneWindow(lane, subagent, time.monotonic())
        for row in brief.strip().splitlines()[:3]:
            window.lines.append(_line((row, "italic dim")))

    def finish(self, lane: str, *, failed: bool, seconds: float) -> None:
        if window := self.windows.get(lane):
            window.status, window.seconds, window.streaming = (
                "failed" if failed else "done",
                seconds,
                "",
            )

    def close(self) -> None:
        if self._live is not None:
            self._live.stop()
            if not self.renderer.console.is_terminal:
                self.renderer.console.line()  # Live adds it itself only on a terminal
        self._live = None

    def log(self, event: AgentEvent, *parts: Any) -> None:
        """Append a row to the event's lane window, badged with its job and tagged
        with the agent (indented by its depth below the lane's own agent)."""
        if window := self.get(event):
            badge = (short_lane(window.lane), f"bold {self.renderer.lane_colour(window.lane)}")
            indent = "  " * max(event.depth - 1, 0)
            agent = (event.agent, f"bold {self.renderer.colour(event.agent)}")
            window.lines.append(_line(badge, (" › ", "dim"), indent, agent, " ", *parts))

    # -- drawing -------------------------------------------------------------------

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        windows = list(self.windows.values())
        if not windows:
            return
        cols = max(1, min(len(windows), options.max_width // self.MIN_WIDTH))
        rows = -(-len(windows) // cols)
        # Equal windows, as tall as the fullest one needs, all fitting on screen.
        needed = max(self._rows(w) for w in windows) + 2
        height = max(6, min(needed, (console.height - 2) // rows))
        grid = Table.grid(expand=True, padding=(0, 1))
        for _ in range(cols):
            grid.add_column(ratio=1)
        panels = [self._panel(w, height) for w in windows]
        for i in range(0, len(panels), cols):
            row = panels[i : i + cols]
            grid.add_row(*row, *[""] * (cols - len(row)))
        yield grid

    @staticmethod
    def _rows(window: LaneWindow) -> int:
        plans = len(window.plans) + 1 if window.plans else 0
        return plans + len(window.lines) + bool(window.streaming)

    def _panel(self, window: LaneWindow, height: int) -> Panel:
        colour = self.renderer.lane_colour(window.lane)
        body = height - 2  # rows inside the border
        top: list[Any] = []
        for agent, todos in list(window.plans.items()):
            done = sum(t.get("status") == "completed" for t in todos)
            current = next((t for t in todos if t.get("status") == "in_progress"), None)
            top.append(
                _line(
                    ("plan ", "bold"),
                    (f"{done}/{len(todos)} ", "green" if done == len(todos) else "yellow"),
                    (agent, self.renderer.colour(agent)),
                    (f"  ◐ {current.get('content', '')}" if current else "", "yellow"),
                )
            )
        top = top[: max(body // 3, 1)]
        if top:
            top.append(Rule(style="dim"))
        tail: list[Text] = []
        if window.streaming:
            tail.append(_line(("💬 ", ""), (window.streaming.splitlines()[-1], "italic")))
        room = max(body - len(top) - len(tail), 0)
        log = list(window.lines)[-room:] if room else []
        rows = [*top, *log, *tail]
        stats = self.renderer.lanes.get(window.lane, {})
        seconds = (
            window.seconds if window.seconds is not None else time.monotonic() - window.started
        )
        icon, style = {
            "running": ("◐ running", "bold yellow"),
            "done": ("✔ done", "bold green"),
            "failed": ("✗ failed", "bold red"),
        }[window.status]
        return Panel(
            Group(*rows),
            title=Text.assemble(
                (f" {window.lane} ", f"bold {colour}"),
                (window.subagent, self.renderer.colour(window.subagent)),
                " ",
            ),
            title_align="left",
            subtitle=Text.assemble(
                (f" {icon} {seconds:.1f}s", style),
                (
                    f" · {stats.get('model', 0)} model / {stats.get('tools', 0)} tools"
                    + (f" / {stats['errors']} errors" if stats.get("errors") else "")
                    + " ",
                    "dim",
                ),
            ),
            subtitle_align="right",
            border_style="red" if window.status == "failed" else colour,
            height=height,
        )


class RichRenderer:
    """Listeners that render every event kind with a Rich console.

    Args:
        console: Rich console (pass `Console(record=True)` to capture output).
        show_nodes: Also show graph nodes (model / tools / middleware steps).
        max_lines: Lines of code / output shown per tool call.
        windows: Stream each parallel lane in its own window of a live grid
            (`LaneWindows`) instead of interleaving the lanes' lines. Default: on
            when the console is an interactive terminal.
    """

    def __init__(
        self,
        console: Console | None = None,
        *,
        show_nodes: bool = False,
        max_lines: int = 12,
        windows: bool | None = None,
    ) -> None:
        self.console = console or Console()
        self.show_nodes = show_nodes
        self.max_lines = max_lines
        self.usage = Counter()
        self.calls: Counter = Counter()
        self._tool_started: dict[str, float] = {}
        self._streaming: str | None = None  # run_id of the model call streaming text
        self._live: Live | None = None
        self._buffer = ""
        self._stream_agent = ""
        self._lane_colours: dict[str, str] = {}
        self.lanes: dict[
            str, dict[str, Any]
        ] = {}  # lane -> subagent, seconds, status, result, counts
        self._batch: list[str] = []  # lanes of the current parallel batch
        self.use_windows = self.console.is_terminal if windows is None else windows
        self.windows = LaneWindows(self)

    def attach(self, stream: EventStream) -> RichRenderer:
        for kind in KINDS:
            stream.on(kind, getattr(self, f"on_{kind}"))
        return self

    # -- helpers -----------------------------------------------------------------

    def colour(self, agent: str) -> str:
        return AGENT_COLOURS.get(agent, "white")

    def lane_colour(self, lane: str) -> str:
        if lane not in self._lane_colours:
            self._lane_colours[lane] = LANE_COLOURS[len(self._lane_colours) % len(LANE_COLOURS)]
        return self._lane_colours[lane]

    def tag(self, event: AgentEvent, *, own_lane: bool = True) -> Text:
        """ "[agent] " for the orchestrator; "[lane › agent] " inside a parallel lane,
        so interleaved lines of concurrent tasks are told apart at a glance.
        `own_lane=False` for the orchestrator starting a lane: it runs the lane, it
        is not inside it."""
        indent = "  " * event.depth
        if not event.lane or not own_lane:
            return Text(f"{indent}[{event.agent}] ", style=f"bold {self.colour(event.agent)}")
        return Text.assemble(
            (f"{indent}[", "bold"),
            (event.lane, f"bold {self.lane_colour(event.lane)}"),
            (" › ", "dim"),
            (event.agent, f"bold {self.colour(event.agent)}"),
            ("] ", "bold"),
        )

    def _lane_stats(self, event: AgentEvent) -> dict[str, Any] | None:
        if not event.lane:
            return None
        return self.lanes.setdefault(event.lane, {"model": 0, "tools": 0, "errors": 0})

    def _answer_panel(self, agent: str, text: str) -> Panel:
        return Panel(
            Markdown(text),
            title=Text(agent, style=f"bold {self.colour(agent)}"),
            title_align="left",
            border_style=self.colour(agent),
        )

    def _end_stream(self) -> None:
        if self._live is not None:
            self._live.update(self._answer_panel(self._stream_agent, self._buffer))
            self._live.stop()
        self._live, self._streaming, self._buffer = None, None, ""

    def _clip(self, text: str) -> str:
        lines = text.splitlines() or [""]
        more = len(lines) - self.max_lines
        return "\n".join(lines[: self.max_lines]) + (f"\n… {more} more lines" if more > 0 else "")

    # -- agents and subagents ------------------------------------------------------

    def on_agent_start(self, event: AgentEvent) -> None:
        self._end_stream()
        if self.windows.get(event):
            return self.windows.log(event, ("▶ started", "bold"))
        if event.depth == 0:
            self.console.rule(Text(f" {event.agent} ", style=f"bold {self.colour(event.agent)}"))
        else:
            self.console.print(Text.assemble(self.tag(event), ("▶ started", "bold")))

    def on_agent_end(self, event: AgentEvent) -> None:
        self._end_stream()
        if self.windows.get(event):
            return self.windows.log(event, ("■ finished", "bold"))
        if event.depth > 0:
            self.console.print(Text.assemble(self.tag(event), ("■ finished", "bold")))

    def on_subagent_start(self, event: AgentEvent) -> None:
        self._end_stream()
        sub = event.data.get("subagent") or "subagent"
        self.calls[f"task→{sub}"] += 1
        running = event.data.get("running")
        brief = str(event.data.get("task") or "")
        if running is None and self.windows.get(event):  # a nested task inside a lane
            return self.windows.log(
                event,
                ("task → ", "bold"),
                (sub, f"bold {self.colour(sub)}"),
                f"  {_first_line(brief)}",
            )
        if running is not None:  # a lane started by the orchestrator
            stats = self._lane_stats(event)
            stats.update(subagent=sub, status="running", started_at=event.data.get("started_at"))
            if event.lane not in self._batch:
                self._batch.append(event.lane)
            if self.use_windows and event.lane:
                self.windows.open(event.lane, sub, brief)
            self.console.print(
                Text.assemble(
                    ("▶ ", "bold"),
                    (event.lane, f"bold {self.lane_colour(event.lane)}"),
                    (f"  {sub} started at {_clock(event.data.get('started_at'))}", "bold"),
                    (f"  ·  {running} running in parallel" if running > 1 else "", "bold yellow"),
                )
            )
            if self.windows.active:  # the brief heads the lane's window
                return
        self.console.print(
            Panel(
                Text(brief, overflow="fold"),
                title=Text.assemble(
                    self.tag(event, own_lane=running is None),
                    ("task → ", "bold"),
                    (sub, f"bold {self.colour(sub)}"),
                    *(
                        [("  ", ""), (event.lane, f"bold {self.lane_colour(event.lane)}")]
                        if running is not None
                        else []
                    ),
                ),
                title_align="left",
                border_style=self.colour(sub),
            )
        )

    def on_subagent_end(self, event: AgentEvent) -> None:
        self._end_stream()
        sub = event.data.get("subagent") or "subagent"
        still = event.data.get("still_running")
        failed = bool(event.data.get("error"))
        if still is None and self.windows.get(event):  # a nested task inside a lane
            return self.windows.log(
                event,
                ("← ", "bold"),
                (sub, f"bold {self.colour(sub)}"),
                (f"  {_first_line(event.data.get('output'))}", "red" if failed else "dim"),
            )
        if still is not None and self.windows.active and event.lane:
            self.windows.finish(event.lane, failed=failed, seconds=event.data.get("seconds") or 0.0)
            if still == 0:  # the last lane: leave the final frame on screen
                self.windows.close()
        title = [(sub, f"bold {self.colour(sub)}"), (" → result", "bold")]
        if event.lane:
            title = [(event.lane, f"bold {self.lane_colour(event.lane)}"), (" › ", "dim"), *title]
        self.console.print(
            Panel(
                Markdown(self._clip(event.data.get("output") or "")),
                title=Text.assemble(*title),
                title_align="left",
                border_style=self.lane_colour(event.lane) if event.lane else "grey50",
            )
        )
        if still is None:  # a nested task (inside a lane), not a lane itself
            return
        seconds = event.data.get("seconds") or 0.0
        output = (event.data.get("output") or "").strip().splitlines()
        self._lane_stats(event).update(
            status="failed" if failed else "done",
            seconds=seconds,
            ended_at=event.data.get("ended_at"),
            result=output[0][:80] if output else "",
        )
        mark, style = ("✗", "bold red") if failed else ("■", "bold")
        self.console.print(
            Text.assemble(
                (f"{mark} ", style),
                (event.lane, f"bold {self.lane_colour(event.lane)}"),
                (f"  {sub} {'failed' if failed else 'finished'} in {seconds:.1f}s", style),
                (f" at {_clock(event.data.get('ended_at'))}", style),
                (f"  ·  {still} still running" if still else "", "bold yellow"),
            )
        )
        if still == 0 and len(self._batch) > 1:
            self._batch_table()
        if still == 0:
            self._batch = []

    def _batch_table(self) -> None:
        stats = [self.lanes.get(lane, {}) for lane in self._batch]
        starts = [st["started_at"] for st in stats if st.get("started_at")]
        ends = [st["ended_at"] for st in stats if st.get("ended_at")]
        first, last = (min(starts), max(ends)) if starts and ends else (None, None)
        table = Table(
            title=f"parallel batch: {len(self._batch)} tasks",
            caption=f"timeline: {_clock(first)} → {_clock(last)}, overlapping bars ran at the same time",
            header_style="bold",
        )
        for column in (
            "lane",
            "agent",
            "status",
            "started",
            "ended",
            "time",
            "timeline",
            "model calls",
            "tool calls",
            "errors",
            "result",
        ):
            table.add_column(
                column,
                justify="right"
                if column in ("time", "model calls", "tool calls", "errors")
                else "left",
                # One line per job: only the result gives way on a narrow terminal.
                no_wrap=column != "result",
                min_width=len("00:00:00") if column in ("started", "ended") else None,
                overflow="ellipsis" if column == "result" else "fold",
            )
        for lane in self._batch:
            st = self.lanes.get(lane, {})
            status = st.get("status", "?")
            table.add_row(
                Text(lane, style=f"bold {self.lane_colour(lane)}"),
                st.get("subagent", ""),
                Text(status, style="red" if status == "failed" else "green"),
                _clock(st.get("started_at")),
                _clock(st.get("ended_at")),
                f"{st.get('seconds', 0.0):.1f}s",
                self._timeline(lane, st, first, last),
                str(st.get("model", 0)),
                str(st.get("tools", 0)),
                str(st.get("errors", 0)),
                st.get("result", ""),
            )
        self.console.print(table)

    TIMELINE_WIDTH = 16

    def _timeline(
        self, lane: str, st: dict[str, Any], first: float | None, last: float | None
    ) -> Text:
        """The lane's run as a bar on the batch's time axis: overlapping bars are
        jobs that ran at the same time."""
        start, end, width = st.get("started_at"), st.get("ended_at"), self.TIMELINE_WIDTH
        if not (start and end and first and last):
            return Text("")
        span = max(last - first, 1e-9)
        a = min(int((start - first) / span * width), width - 1)
        b = max(round((end - first) / span * width), a + 1)
        return Text.assemble(
            ("·" * a, "dim"), ("█" * (b - a), self.lane_colour(lane)), ("·" * (width - b), "dim")
        )

    # -- graph nodes -----------------------------------------------------------------

    def on_node_start(self, event: AgentEvent) -> None:
        if self.show_nodes:
            self._end_stream()
            if self.windows.get(event):
                return self.windows.log(event, (f"· {event.name}", "dim"))
            self.console.print(Text.assemble(self.tag(event), (f"· {event.name}", "dim")))

    def on_node_end(self, event: AgentEvent) -> None:
        return None

    # -- model calls ------------------------------------------------------------------

    def on_model_start(self, event: AgentEvent) -> None:
        self.calls[f"model:{event.agent}"] += 1
        if (stats := self._lane_stats(event)) is not None:
            stats["model"] += 1
        if self.show_nodes:
            self._end_stream()
            model = event.data.get("model") or "model"
            if self.windows.get(event):
                return self.windows.log(event, (f"🧠 {model}", "dim"))
            self.console.print(
                Text.assemble(
                    self.tag(event),
                    (f"🧠 {model} ({event.data.get('messages', 0)} messages)", "dim"),
                )
            )

    def on_model_token(self, event: AgentEvent) -> None:
        """Stream the orchestrator's text live, rendered as Markdown as it arrives;
        a lane's text streams into the bottom row of its window."""
        if window := self.windows.get(event):
            window.streaming += event.data.get("text", "")
            return
        if event.depth > 0:  # subagents: their answer is shown when the task returns
            return
        if self.windows.active:  # one Live at a time: printed whole at model_end
            return
        if self._streaming != event.run_id:
            self._end_stream()
            self._streaming, self._stream_agent = event.run_id, event.agent
            self._live = Live(console=self.console, refresh_per_second=12, transient=False)
            self._live.start()
        self._buffer += event.data.get("text", "")
        self._live.update(self._answer_panel(self._stream_agent, self._buffer))

    def on_model_end(self, event: AgentEvent) -> None:
        streamed = self._streaming == event.run_id
        self._end_stream()
        usage = event.data.get("usage") or {}
        cached = (usage.get("input_token_details") or {}).get("cache_read", 0) or 0
        self.usage["input"] += usage.get("input_tokens", 0) or 0
        self.usage["output"] += usage.get("output_tokens", 0) or 0
        self.usage["cached"] += cached
        text, tool_calls = event.data.get("text") or "", event.data.get("tool_calls") or []
        if window := self.windows.get(event):
            window.streaming = ""
            if text.strip():
                self.windows.log(event, ("💬 ", ""), (_first_line(text), "italic"))
            return
        if event.depth == 0 and text and not tool_calls and not streamed:
            self.console.print(self._answer_panel(event.agent, text))
        tasks = [c for c in tool_calls if c.get("name") == "task"]
        if len(tasks) > 1:  # several delegations in one message: they run in parallel
            self._parallel_panel(event, tasks)
        if usage and self.show_nodes:
            line = (
                f"tokens in {usage.get('input_tokens', 0):,} (cached {cached:,}) "
                f"out {usage.get('output_tokens', 0):,}"
            )
            self.console.print(Text.assemble(self.tag(event), (line, "dim")))

    def _parallel_panel(self, event: AgentEvent, tasks: list[dict]) -> None:
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("lane")
        table.add_column("agent")
        table.add_column("brief")
        for n, call in enumerate(tasks, 1):
            args = call.get("args") or {}
            lane = lane_label(args.get("description"), args.get("subagent_type"), n)
            first = str(args.get("description") or "").strip().splitlines()[:1]
            table.add_row(
                Text(lane, style=f"bold {self.lane_colour(lane)}"),
                Text(
                    str(args.get("subagent_type")),
                    style=self.colour(str(args.get("subagent_type"))),
                ),
                (first[0][:70] if first else ""),
            )
        self.console.print(
            Panel(
                table,
                title=Text.assemble(
                    self.tag(event), (f"⇉ {len(tasks)} tasks launched in parallel", "bold yellow")
                ),
                title_align="left",
                border_style="yellow",
            )
        )

    # -- tools ------------------------------------------------------------------------

    def _args_view(self, name: str, args: dict) -> Any:
        if name == "execute":
            return Syntax(self._clip(str(args.get("command", ""))), "bash", word_wrap=True)
        if name in ("write_file", "edit_file"):
            path = str(args.get("file_path", ""))
            body = args.get("content") or args.get("new_string") or ""
            lexer = next((v for k, v in _CODE_EXT.items() if path.endswith(k)), "text")
            return Syntax(self._clip(str(body)), lexer, word_wrap=True) if body else None
        if name == "browser_evaluate":
            return Syntax(self._clip(str(args.get("function", ""))), "javascript", word_wrap=True)
        rest = {k: v for k, v in args.items() if k not in ("file_path", "url")}
        return Syntax(self._clip(json.dumps(rest, indent=2, default=str)), "json") if rest else None

    def _headline(self, name: str, args: dict) -> str:
        for key in ("file_path", "path", "url", "pattern"):
            if args.get(key):
                return f"{name} {args[key]}"
        return name

    def on_tool_start(self, event: AgentEvent) -> None:
        self._end_stream()
        self.calls[f"tool:{event.name}"] += 1
        if (stats := self._lane_stats(event)) is not None:
            stats["tools"] += 1
        self._tool_started[event.run_id] = time.monotonic()
        args = event.data.get("args") or {}
        if self.windows.get(event):
            command = _first_line(args.get("command")) if event.name == "execute" else ""
            return self.windows.log(
                event,
                ("🔧 ", ""),
                (self._headline(event.name, args), "bold"),
                (f"  $ {command}" if command else "", "dim"),
            )
        headline = Text.assemble(
            self.tag(event), ("🔧 ", ""), (self._headline(event.name, args), "bold")
        )
        view = self._args_view(event.name, args)
        if view is None:
            self.console.print(headline)
            return
        # The call's code / arguments box carries the "[lane › agent]" tag too, so a
        # box from a parallel job is never anonymous among the others' output.
        self.console.print(
            Panel(
                view,
                title=headline,
                title_align="left",
                border_style=self.lane_colour(event.lane)
                if event.lane
                else self.colour(event.agent),
                padding=(0, 1),
            )
        )

    def _elapsed(self, event: AgentEvent) -> str:
        started = self._tool_started.pop(event.run_id, None)
        return f" {time.monotonic() - started:.1f}s" if started is not None else ""

    def on_tool_end(self, event: AgentEvent) -> None:
        self._end_stream()
        preview = _first_line(event.data.get("output"), 160)
        if self.windows.get(event):
            return self.windows.log(
                event,
                ("✓ ", "green"),
                (event.name, "green"),
                (self._elapsed(event), "dim"),
                (f"  {preview}" if preview else "", "dim"),
            )
        self.console.print(
            Text.assemble(
                self.tag(event),
                ("✓ ", "green"),
                (event.name, "green"),
                (self._elapsed(event), "dim"),
                (f"  {preview}" if preview else "", "dim"),
            )
        )

    def on_tool_error(self, event: AgentEvent) -> None:
        self._end_stream()
        self.calls["errors"] += 1
        if (stats := self._lane_stats(event)) is not None:
            stats["errors"] += 1
        if self.windows.get(event):
            return self.windows.log(
                event,
                (f"✗ {event.name}{self._elapsed(event)}  ", "bold red"),
                (_first_line(event.data.get("output")), "red"),
            )
        self.console.print(
            Panel(
                Text(self._clip(event.data.get("output") or ""), overflow="fold"),
                title=Text.assemble(
                    self.tag(event), (f"✗ {event.name}{self._elapsed(event)}", "bold red")
                ),
                title_align="left",
                border_style="red",
            )
        )

    # -- plan --------------------------------------------------------------------------

    def on_todos(self, event: AgentEvent) -> None:
        self._end_stream()
        todos = event.data.get("todos") or []
        if window := self.windows.get(event):
            window.plans[event.agent] = todos
            return
        done = sum(t.get("status") == "completed" for t in todos)
        table = Table(show_header=False, box=None, padding=(0, 1))
        for todo in todos:
            icon, style = _TODO.get(todo.get("status"), ("○", "grey50"))
            table.add_row(Text(icon, style=style), Text(str(todo.get("content", "")), style=style))
        self.console.print(
            Panel(
                table,
                title=Text.assemble(self.tag(event), (f"plan {done}/{len(todos)}", "bold")),
                title_align="left",
                border_style=self.colour(event.agent),
            )
        )

    def on_custom(self, event: AgentEvent) -> None:
        self._end_stream()
        if self.windows.get(event):
            return self.windows.log(
                event, (f"• {event.name}: {event.data.get('payload')}", "italic")
            )
        self.console.print(
            Text.assemble(
                self.tag(event), (f"• {event.name}: {event.data.get('payload')}", "italic")
            )
        )

    # -- end of a turn -------------------------------------------------------------------

    def summary(self) -> None:
        self._end_stream()
        self.windows.close()  # the run stopped or failed with lanes still open
        table = Table(title="run summary", show_header=True, header_style="bold")
        table.add_column("what")
        table.add_column("count", justify="right")
        for key, count in sorted(self.calls.items()):
            table.add_row(key, str(count))
        if self.usage["input"]:
            share = 100 * self.usage["cached"] / self.usage["input"]
            table.add_row(
                "input tokens (cached)",
                f"{self.usage['input']:,} ({self.usage['cached']:,}, {share:.0f}%)",
            )
            table.add_row("output tokens", f"{self.usage['output']:,}")
        for lane, st in self.lanes.items():
            table.add_row(
                Text(f"lane {lane}", style=f"bold {self.lane_colour(lane)}"),
                f"{st.get('status', '?')} {st.get('seconds', 0.0):.1f}s "
                f"({_clock(st.get('started_at'))} → {_clock(st.get('ended_at'))}) · "
                f"{st.get('model', 0)} model / {st.get('tools', 0)} tool calls",
            )
        self.console.print(table)


__all__ = [
    "AGENT_COLOURS",
    "KINDS",
    "LANE_COLOURS",
    "AgentEvent",
    "EventStream",
    "LaneWindow",
    "LaneWindows",
    "Listener",
    "RichRenderer",
    "TaskRun",
    "lane_label",
    "short_lane",
]
