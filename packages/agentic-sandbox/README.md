# genai-agentic-sandbox

**JobHunter**: a Deep Agent (LangChain `deepagents` + OpenAI) that finds jobs with a
real browser, scores them against your resume with an ATS review, picks the best
N (default 2), and for each one improves a copy of your resume in rounds - the
resume builder edits, an independent ATS reviewer scores and gives feedback -
until it reaches the target score (default 85), stops improving, or hits the
round limit (default 3).
Agents work by **writing Python at run time and executing it in a Docker
sandbox** (python-docx, lxml, pandas preinstalled); scripts and results land in
the mounted output folder. The only non-sandbox tools are the Playwright MCP
browser tools, because the sandbox has no network.

```
jobhunter (main, orchestrator)
├── job-search       Playwright MCP browser tools       -> /output/jobs/jobs.json
├── job-matcher      ATS scoring, best N jobs            -> /output/match/match_report.{json,md}
├── job-optimizer    ONE PER JOB, ALL IN PARALLEL        -> /output/resume/<rank>-<slug>/
│   ├── resume-builder   round k: redline / review / final .docx
│   └── ats-reviewer     round k: fixed ATS score + feedback (loop until done)
└── ats-reviewer     score-only workflow (your resume as it is)
```

For N selected jobs the orchestrator launches N job-optimizers in one message;
each owns its job's whole builder <-> reviewer loop, so two jobs take about as
long as one.

## Run

```bash
cp ../../.env.example ../../.env        # set OPENAI_API_KEY (LLM_MODEL defaults to gpt-5.5)
npx @playwright/mcp@latest install-browser chromium   # once

uv run genai-agentic-sandbox --resume ~/cv.docx --out ./jobhunt-output \
  "find senior python backend jobs in Kochi, match my resume and revise it"
uv run genai-agentic-sandbox --resume ~/cv.docx     # interactive session
```

Options: `--top-jobs N` (default 2), `--target-score` (85), `--max-rounds` (3),
`--keep-work-files`, `--memory-dir` (default `<out>/memories`), `--no-memory`,
`--prompt-cache-retention {in_memory,24h}`, `--verbose`, `--model`, `--headed`, `--no-verify`.

Your resume is never modified. Before anything runs it is checked (a .docx with
real text - a blank document is rejected with an error), copied to
`<out>/original/` (read-only), and only that copy is mounted, read-only, at
`/input` and `/output/original`. Output:

```
jobhunt-output/
├── original/<your resume>.docx            read-only copy
├── memories/user_profile.md, agent_notes.md   long-term memory (never deleted)
├── jobs/jobs.json                          found jobs
├── match/match_report.{json,md}            scores, ATS review, selected_jobs
└── resume/<rank>-<company-role>/           one folder per selected job
    ├── <name>_<slug>_redline.docx        colour-coded changes (read this)
    ├── <name>_<slug>_review.docx         same changes as Word tracked changes (accept/reject)
    ├── <name>_<slug>_final.docx          clean, ATS-ready, everything applied (send this)
    ├── ats_history.json                  ATS score per round, best round
    ├── change_log.md                     what changed per round and why; open questions
    ├── revise_resume.py                  the builder's script
    ├── v1/ v2/ ...                       every round's three files
    └── ats/                              ats_score.py, job.json, round-<k>.json feedback
```

Colours in the redline copy: **green** added, **red struck** removed or replaced
(old text), **blue** rephrased (new text), **purple** moved / reordered,
**yellow shading** formatting or style change, **dark red on pink, double
underline** suspected wrong data (the comment gives the alternative). Word shows
tracked changes in the reviewer's markup colour, which is why the colours live
in the separate redline copy; Reject All on the review copy restores your
original exactly.

What the sandbox container sees: the resume copy (read-only), `/output`
(read-write -> `--out`), `/skills` (read-only). The browser (Playwright MCP) is the only
part on the host, and it is confined: it runs in `<out>/.browser/captures/`
(files it saves land there and are readable in the sandbox at
`/output/.browser/captures/`), every `filename` is reduced to a plain name in that
folder, and the tools that could read host files or run host code
(`browser_file_upload`, `browser_drop`, `browser_run_code_unsafe`) are not loaded. No network. The browser
runs on the host through Playwright MCP and only the job-search subagent has it.

## Workflows (the orchestrator's skills)

The main agent is an orchestrator: it reads `agent-orchestration`, routes the
request, and follows the matching workflow skill (`skills/orchestrator/`):

| Request | Workflow |
|---|---|
| find jobs + tailor resume | `jobhunt-workflow` -> `resume-optimization` -> `output-cleanup` |
| tailor for a job I give (URL / pasted description) | `job-posting-intake` -> `resume-optimization` -> `output-cleanup` |
| ATS score only, no edits | (`job-posting-intake`) -> `ats-score-only` -> `output-cleanup` (result: `ats/ats_report.docx`) |
| only find jobs | `jobhunt-workflow` step 1 |
| clean up | `output-cleanup` |

`output-cleanup` runs last (dry run first) and deletes every working file,
keeping only the .docx deliverables; pass `--keep-work-files` to keep scripts,
JSON, round folders and logs.

## Live view (event stream v2 + Rich)

`streaming.py` consumes `astream_events(..., version="v2")`, classifies every
event and sends it to the listener for its kind; `RichRenderer` draws each kind
in its own style, one colour per agent:

| kind | shown as |
|---|---|
| `agent_start` / `agent_end` | a rule for the orchestrator; "▶ started / ■ finished" for subagents (indented by depth) |
| `subagent_start` / `subagent_end` | a panel "task → <subagent>" with the brief; a result panel with its answer |
| `model_start` / `model_token` / `model_end` | the orchestrator's answer streamed live as Markdown; token and cache usage |
| `tool_start` / `tool_end` / `tool_error` | the call with syntax-highlighted code / command / arguments; ✓ with timing; ✗ in red |
| `todos` | the agent's plan as a table (✔ ◐ ○) every time it changes |
| `node_start` / `node_end` | graph steps (model, tools, middleware) - with `--verbose` |
| `custom` | custom events dispatched by tools or middleware |

**Parallel lanes.** Every task the orchestrator delegates is a lane with a
label - the brief's `[job 1: acme-backend]` prefix (the orchestrator is told to
add one), else the job folder, else `<agent> #n`. Every line from inside a lane
is tagged `[lane › agent]` in the lane's own colour, including the nested
builder and reviewer and the title of every tool call's code / arguments box, so interleaved output of N concurrent job optimizers stays
readable. A panel announces "⇉ N tasks launched in parallel", each lane prints
"▶ started at 14:02:11 · N running in parallel" and "■ finished in 42.3s at
14:02:53 · M still running" (✗ in red if it failed), and a table closes the
batch: lane, agent, status, started, ended, time, a timeline bar on the batch's
time axis (overlapping bars ran at the same time), model / tool calls, errors,
result. The run summary repeats each lane's start → end.

**One window per job.** In an interactive terminal the lanes do not interleave:
while they run, a live grid (`LaneWindows`) gives each lane its own window, side
by side (as many columns as fit, 56+ characters each). A window shows the lane's
brief, one plan line per agent (`plan 2/5 job-optimizer ◐ round 2: ats-reviewer`),
a scrolling log of its agents' steps, every row badged with its job (`job 1 › …`) (nested builder / reviewer indented: task →,
🔧 tool, ✓ / ✗ result, 💬 answer) and, at the bottom, the model text streaming
in that lane right now; the footer has its status, time and model / tool calls.
The orchestrator's own lines and each lane's result panel print above the grid,
and the final frame stays on screen when the last lane ends. `--no-windows` (or
`RichRenderer(windows=False)`, the default when output is not a terminal) keeps
the interleaved `[lane › agent]` lines.

Each turn ends with a summary table (model and tool calls per agent, errors,
input tokens and how many came from the prompt cache, one line per lane). Your own listeners can
subscribe too: `stream.on("tool_error", my_handler)`.

## Planning: todos, completed to the end

Every agent plans with `write_todos` and must finish its list
(`TodoCompletionMiddleware`, `middleware.py`):

- **Plan first** - the tools that do the real work are refused until the agent
  has written its todo list in the current request: `task` for the
  orchestrator; `execute` / `write_file` / `edit_file` (and `browser_navigate`
  for job-search) for subagents. Reading and inspecting are always allowed.
- **Finish every todo** - an agent that tries to answer while an item is
  pending or in_progress is sent back with the open items, every time, until
  each is completed or explicitly removed with a reason (safety cap: 10).
- Both reset per request, so an old completed list never counts as a new plan.

The todo list is the plan - the single source of truth, no plan files (skill
`workflow-planning`). The orchestrator revises it with `write_todos` whenever
results or your new input change the plan; the terminal shows it live.

## Memory

Two Markdown files in the visible folder `<out>/memories/` (e.g.
`jobhunt-output/memories/`, mounted at `/memories`) survive between runs and are
never deleted: `output-cleanup` protects the folder, a guard middleware refuses
any `delete` or shell command that would remove, move or wipe it (including
`rm -rf /output`), and subagents cannot write to it at all. Memory from the old
hidden `~/.jobhunter/memory` is copied in on the first run. Each run prints the
memory files at the start and the end.

| File | Holds | Loaded by |
|---|---|---|
| `user_profile.md` | preferences and facts you confirmed (answers to the resume questions, links, roles, locations) | orchestrator, job-search, job-matcher, resume-builder |
| `agent_notes.md` | lessons: job sites that work/block, changes that raised ATS scores | orchestrator, job-search |

The orchestrator saves what you tell it and curates the subagents' "Memory
notes" (skill `agent-memory`); subagents read memory but do not edit it; the
ATS reviewer loads none, so scores stay independent. Edit `user_profile.md`
yourself to answer last run's questions - the builder then uses those facts
instead of asking again. Never put credentials in it.

## Prompt caching

OpenAI caches automatically: a request of 1024+ tokens whose beginning matches a
recent request is served from cache (cheaper, faster). `caching.py` makes the
most of it - each agent sends its own `prompt_cache_key` (`jobhunter:<agent>`),
optional `prompt_cache_retention` (`24h` on supported models), and prompts keep
a stable prefix (memory is appended at the end). Each turn prints how many
input tokens came from the cache. Deep Agents' Anthropic prompt-caching
middleware is removed via the `openai` harness profile.

## Middleware

`create_deep_agent` already provides `FilesystemMiddleware`, `SubAgentMiddleware`,
automatic summarization and `SkillsMiddleware` (via `skills=`), so they are
configured rather than added again. Added to the main agent and every subagent:
`TodoListMiddleware` (`write_todos`), `OpenAIPromptCachingMiddleware`,
read-only `MemoryMiddleware` (subagents that need memory), `TodoCompletionMiddleware`
(`middleware.py`: an agent that tries to finish with open todos is sent back to
complete or close them) and `create_summarization_tool_middleware`
(`compact_conversation`). Every prompt carries the same todo rules: plan first,
one item per step, in_progress -> completed as you go, nothing open at the end.

## Resume revisions

No revision tool: the resume builder writes `revise_resume.py` (python-docx +
lxml, from the patterns in `skills/resume/docx-tracked-revisions/SKILL.md`),
runs it with `execute`, and verifies that rejecting all changes restores the
original and accepting them equals the final copy. The ATS reviewer writes
`ats_score.py` from `skills/ats/ats-scoring/SKILL.md` once and reuses it every
round, so scores are comparable. Tests run the skills' code on the host, in the
sandbox, through LibreOffice, and through the full loop with a scripted model.

## Layout

```
src/genai_agentic_sandbox/
├── main.py            # CLI: sandbox + Playwright MCP + agent
├── agent.py           # create_jobhunter_agent, subagents, middleware
├── prompts.py         # prompts and the /output file contract
├── middleware.py      # TodoCompletionMiddleware
├── caching.py         # OpenAI prompt caching; Anthropic caching removed
├── streaming.py       # event stream v2 -> listeners per kind -> Rich console
├── memory.py          # long-term memory files, seeding, subagent memory, guard
├── skills/            # SKILL.md files, mounted at /skills
│   ├── orchestrator/  # main agent: agent-orchestration, jobhunt-workflow,
│   │                  # resume-optimization, ats-score-only, job-posting-intake,
│   │                  # output-cleanup, agent-memory, workflow-planning
│   ├── search/web-job-search/
│   ├── matching/ats-resume-review/
│   ├── resume/docx-tracked-revisions/
│   └── ats/ats-scoring/
├── tools/
│   └── browser.py     # Playwright MCP session (one per run) - the only custom tools
├── builder/           # sandbox image builder (`sandbox-build`)
└── sandbox/docker.py  # DockerSandboxBackend
```

## Tests

```bash
uv run pytest packages/agentic-sandbox/tests                 # unit (no Docker)
uv run pytest -m integration packages/agentic-sandbox/tests  # real Docker, scripted model
```

## Sandbox image

The sandbox has no network, so its Python packages are baked into the image from a
pyproject file (default: `src/genai_agentic_sandbox/builder/sandbox.pyproject.toml`).

```bash
uv run sandbox-build                                   # build default image, prints tag
uv run sandbox-build --pyproject path/to/pyproject.toml --apt git
uv run sandbox-build --force --pull                    # rebuild from a fresh base image
uv run sandbox-build --print-dockerfile
```

Env vars: see `SANDBOX_*`, `OPENAI_API_KEY`, `LLM_MODEL`, `POSTGRES_DSN` in the root `.env.example`.
