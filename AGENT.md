# AGENT.md

Guidance for AI coding agents (and humans) working in this repository.

## Purpose

This repository implements the common **GenAI workflows** as independent, runnable Python packages:
RAG, agentic flows, multi-agent systems, prompt optimization, memory, evaluation, and so on.

Core stack:

| Concern                | Technology                         |
|------------------------|------------------------------------|
| LLM orchestration      | **LangChain** / LangGraph          |
| Multi-agent            | **AutoGen**                        |
| Prompt programming/opt | **DSPy**                           |
| Vector store           | **Qdrant**                         |
| Relational / state     | **PostgreSQL**                     |
| Cache / memory / queue | **Redis**                          |
| Package manager        | **uv** (workspace / multi-package) |

## Repository layout (uv workspace)

The repo is a **uv workspace**. The root `genai/` folder has its own `pyproject.toml`, and every
workflow lives in its own subfolder under `packages/` with **its own `pyproject.toml`**.

```
genai/
├── AGENT.md
├── pyproject.toml            # workspace root: members list + shared dev tools
├── uv.lock                   # ONE lockfile for the whole workspace (commit it)
├── .python-version
├── .env.example              # all env vars used by any package
├── docker-compose.yml        # postgres, redis, qdrant for local dev
└── packages/
    ├── core/                 # genai-core: settings, LLM factory, db/redis/qdrant clients
    │   ├── pyproject.toml
    │   ├── src/genai_core/
    │   └── tests/
    ├── agent-flow/           # genai-agent-flow: LangGraph tool-calling agent
    │   ├── pyproject.toml
    │   ├── README.md
    │   ├── src/genai_agent_flow/
    │   └── tests/
    ├── rag/                  # genai-rag: ingestion + retrieval over Qdrant
    ├── multi-agent/          # genai-multi-agent: AutoGen teams
    ├── prompt-opt/           # genai-prompt-opt: DSPy programs + optimizers
    ├── memory/               # genai-memory: Redis short-term / Postgres long-term memory
    └── ...                   # one folder per new workflow
```

### Planned workflows

Add one package per workflow. Suggested set:

| Folder            | Workflow                                                      | Main libs                      |
|-------------------|---------------------------------------------------------------|--------------------------------|
| `core`            | Shared config, clients, logging (no business logic)           | pydantic-settings, sqlalchemy  |
| `rag`             | Chunk → embed → Qdrant → retrieve → rerank → answer           | langchain, qdrant-client       |
| `agent-flow`      | Tool-calling / ReAct agent, human-in-the-loop, checkpoints    | langgraph, postgres checkpoint |
| `multi-agent`     | Planner/worker/critic teams, group chat                       | autogen-agentchat              |
| `prompt-opt`      | Signatures, modules, optimizers (MIPRO, BootstrapFewShot)      | dspy                           |
| `memory`          | Conversation memory, semantic cache                           | redis, postgres                |
| `structured-out`  | Extraction / classification to Pydantic models                 | langchain                      |
| `evals`           | LLM-as-judge, RAG metrics, regression datasets                 | dspy / langchain               |
| `guardrails`      | Input/output validation, PII redaction                        | langchain middleware           |

## Naming conventions

For a workflow folder `packages/<name>/`:

- Distribution name (in `pyproject.toml`): `genai-<name>` (e.g. `genai-agent-flow`)
- Import package: `genai_<name>` under `src/` (e.g. `src/genai_agent_flow/`)
- Use the **src layout** and a `tests/` folder in every package.
- Entry point (optional): `[project.scripts] genai-<name> = "genai_<name>.main:main"`

## Package manager: uv only

- **Never** use `pip`, `poetry`, `pip-tools` or `requirements.txt`. Use `uv` for everything.
- Never hand-edit dependency lists when a `uv add` command can do it.
- Always commit `uv.lock`. Never commit `.venv/`.
- Python version: **3.12** (pinned in `.python-version` and `requires-python = ">=3.12"`).

### Root `pyproject.toml`

```toml
[project]
name = "genai"
version = "0.1.0"
requires-python = ">=3.12"
# Root depends on every member, so a plain `uv sync` at the root installs everything.
dependencies = [
    "genai-core",
    "genai-agent-flow",
    # add each new package here
]

[tool.uv.workspace]
members = ["packages/*"]

[tool.uv.sources]
genai-core = { workspace = true }
genai-agent-flow = { workspace = true }
# add each new package here

[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio", "ruff", "mypy", "ipykernel"]

[tool.ruff]
line-length = 100
target-version = "py312"
```

### Member `pyproject.toml` (e.g. `packages/agent-flow/pyproject.toml`)

```toml
[project]
name = "genai-agent-flow"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "genai-core",
    "langchain",
    "langgraph",
]

[tool.uv.sources]
genai-core = { workspace = true }

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

## Installing dependencies

A uv workspace shares **one lockfile and one `.venv` at the root**, so dependency versions stay
consistent across packages. You can install from the root or from inside a subfolder.

**From the root (everything):**

```bash
uv sync                  # root project -> installs all members listed in root dependencies
uv sync --all-packages   # explicit: every workspace member, even ones not listed in root
```

**Only one package, from the root:**

```bash
uv sync --package genai-agent-flow
```

**From inside a subfolder:**

```bash
cd packages/agent-flow
uv sync                  # installs genai-agent-flow + its deps (incl. genai-core) into root .venv
uv sync --inexact        # same, but keeps the other packages already installed in .venv
uv run pytest
```

> Note: plain `uv sync` makes the shared `.venv` match exactly that target, so running it inside
> one package **uninstalls** the other packages. Use `--inexact` if you want to keep them, or run
> `uv sync --all-packages` from the root to get everything back.

## Creating a new workflow package

Example: starting the agent flow.

```bash
# from the repo root
uv init --lib packages/agent-flow --name genai-agent-flow
# creates packages/agent-flow/pyproject.toml + src/genai_agent_flow/ (already a member via packages/*)

uv add --package genai-agent-flow genai-core langchain langgraph
# then add it to the root pyproject: dependencies + [tool.uv.sources] (workspace = true)
uv sync
```

Checklist for every new package:

1. `pyproject.toml` with name `genai-<name>`, src layout, `tests/` folder.
2. Depends on `genai-core` via `{ workspace = true }` for config and clients — do not
   re-implement connection code.
3. Registered in the root `pyproject.toml` (`dependencies` + `[tool.uv.sources]`).
4. A short `README.md` explaining the workflow, how to run it, and required env vars.
5. New env vars added to the root `.env.example`.
6. At least one test runnable with `uv run --package genai-<name> pytest`.

## Adding / removing dependencies

```bash
uv add --package genai-rag qdrant-client langchain-qdrant   # runtime dep for one package
uv add --dev pytest-cov                                      # shared dev tool (root)
uv remove --package genai-rag some-lib
uv lock --upgrade-package langchain                          # bump one lib
```

Put a dependency in the **package that uses it**, not in the root. Only shared dev tooling
belongs in the root `dev` group.

## Running

```bash
uv run --package genai-agent-flow python -m genai_agent_flow.main
uv run --package genai-agent-flow genai-agent-flow           # if a script entry point exists
uv run pytest                                                # all tests
uv run --package genai-rag pytest packages/rag/tests         # one package
uv run ruff check . && uv run ruff format .
uv run mypy packages
```

## Infrastructure (local)

`docker-compose.yml` at the root provides the backing services:

| Service  | Image                      | Port        | Used for                                          |
|----------|----------------------------|-------------|---------------------------------------------------|
| postgres | `pgvector/pgvector:pg16`   | 5432        | LangGraph checkpoints, chat history, app data     |
| redis    | `redis/redis-stack`        | 6379        | Cache, semantic cache, short-term memory, queues  |
| qdrant   | `qdrant/qdrant`            | 6333 / 6334 | Vector search (HTTP / gRPC)                       |

```bash
docker compose up -d
cp .env.example .env
```

## Configuration

- All settings are loaded in `genai-core` via `pydantic-settings` from environment / `.env`.
- Expected variables (extend in `.env.example`):
  `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `LLM_PROVIDER`, `LLM_MODEL`, `EMBEDDING_MODEL`,
  `POSTGRES_DSN`, `REDIS_URL`, `QDRANT_URL`, `QDRANT_API_KEY`.
- Never hard-code keys, URLs or model names in workflow packages; read them from `genai_core.settings`.
- Never commit `.env`.

## Code guidelines

- Type hints everywhere; Pydantic models for structured inputs/outputs.
- Prefer async clients (`AsyncQdrantClient`, `redis.asyncio`, `asyncpg`/`psycopg` async) in I/O paths.
- Keep framework code at the edges: a workflow's core logic should be testable without a live LLM
  (inject the model / use fakes in tests).
- Tests that need Postgres/Redis/Qdrant or a real LLM are marked `@pytest.mark.integration`
  and skipped by default.
- Packages must not import from each other except `genai-core` (or an explicitly declared
  workspace dependency).
- Format and lint with `ruff`; code must pass `ruff check` and `mypy` before commit.

## Agent do / don't

- **Do** run `uv sync` after changing any `pyproject.toml`, and commit the updated `uv.lock`.
- **Do** check the latest library docs (LangChain, AutoGen, DSPy, Qdrant) — these APIs change often.
- **Do** use the vendored Qdrant skills in `.claude/skills/qdrant-*` for any Qdrant design decision
  (sizing, quantization, hybrid search, multitenancy, scaling). See `.claude/skills/QDRANT-SKILLS.md`.
- **Don't** create `requirements.txt`, `setup.py`, or per-package `uv.lock` / `.venv` files.
- **Don't** add dependencies to the root `[project].dependencies` other than workspace members.
