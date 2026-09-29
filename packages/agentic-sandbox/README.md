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
jobhunter (main)                  write_todos, task, fs tools, execute
├── job-search      Playwright MCP browser tools   -> /output/jobs/jobs.json
├── job-matcher     ATS scoring, required skills   -> /output/match/match_report.{json,md}
├── resume-builder  per job, per round             -> redline / review / final .docx
└── ats-reviewer    per job, per round             -> fixed ATS score + feedback (loop until done)
```

## Run

```bash
cp ../../.env.example ../../.env        # set OPENAI_API_KEY (LLM_MODEL defaults to gpt-5.5)
npx @playwright/mcp@latest install-browser chromium   # once

uv run genai-agentic-sandbox --resume ~/cv.docx --out ./jobhunt-output \
  "find senior python backend jobs in Kochi, match my resume and revise it"
uv run genai-agentic-sandbox --resume ~/cv.docx     # interactive session
```

Options: `--top-jobs N` (default 2), `--target-score` (85), `--max-rounds` (3),
`--keep-work-files`, `--memory-dir` (`~/.jobhunter/memory`), `--no-memory`,
`--prompt-cache-retention {in_memory,24h}`, `--model`, `--headed`, `--no-verify`.

Your resume is never modified. Before anything runs it is checked (a .docx with
real text - a blank document is rejected with an error), copied to
`<out>/original/` (read-only), and only that copy is mounted, read-only, at
`/input` and `/output/original`. Output:

```
jobhunt-output/
├── original/<your resume>.docx            read-only copy
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
(read-write -> `--out`), `/skills` (read-only). No network. The browser
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

## Memory

Two Markdown files in `~/.jobhunter/memory` (mounted at `/memories`) survive
between runs; `output-cleanup` never touches them:

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
├── memory.py          # long-term memory files, seeding, subagent memory
├── skills/            # SKILL.md files, mounted at /skills
│   ├── orchestrator/  # main agent: agent-orchestration, jobhunt-workflow,
│   │                  # resume-optimization, ats-score-only, job-posting-intake,
│   │                  # output-cleanup, agent-memory
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
