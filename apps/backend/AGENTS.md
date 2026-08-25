# ForgeAI Backend Agent Guide

Rules in this file apply when working under `apps/backend/`.

## Stack

- Use Python 3.13+, FastAPI, SQLAlchemy, Pydantic, Alembic, and uv.
- Prefer type annotations on public functions and service boundaries.
- Format and lint with Ruff; type-check with mypy. Do not bypass these tools for convenience.

## Reuse first

- Before changing code, search for existing services, agents, schemas, models, exceptions, and tests.
- Prefer composing or extending what already exists. Do not copy a module and rename it.
- Before adding a new service or agent, confirm no existing module already owns the same responsibility.
- If the same logic appears twice, consider extracting a helper; if it appears three times, extract it.
- Keep LLM prompt text in `app/agents/prompts/`; do not scatter long system prompts inside routers.

## API and domain conventions

- Return responses through the shared `ApiResponse` / `success(...)` helpers unless there is a clear exception.
- Raise `AppException` subclasses (`BusinessException`, `NotFoundException`, …) for expected failures; let the global handlers format them.
- Preserve project status transitions (draft → PRD / spec → approval → build → validation / repair → completed or failed). Do not invent silent status jumps.
- Generated websites are the three-file prototype (`index.html` / `style.css` / `script.js`) unless the approved spec and platform explicitly support more.
- Treat generated HTML/JS as untrusted content at the product boundary; do not weaken preview isolation assumptions when changing editor or builder output.
- Keep frontend/backend schema fields aligned. If you rename or reshape API fields, update both sides or document the break.

## Data and migrations

- Schema changes require an Alembic revision under `alembic/versions/`.
- Migrations must be UTF-8 and keep a valid single-head ancestry unless intentionally branching.
- Prefer additive, reversible-friendly changes for existing rows (defaults, backfills) over destructive rewrites.
- Do not call paid model APIs or mutate a developer database just to “try” a change during implementation unless the user asks.

## Change discipline

- Before editing, briefly state which existing modules will be reused or changed.
- Touch only files needed for the current request.
- Do not casually rewrite whole services or agents.
- Do not remove existing user functionality or unrelated local changes.
- When fixing a concrete case, decide first whether it is a general defect or a one-off special case.
- Prefer deterministic checks (parsers, validators, unit tests) before relying on another LLM pass.

## Encoding and LLM output

- All source and log files must be UTF-8. Do not introduce mojibake or mixed encodings.
- Parse LLM JSON defensively; handle truncation and malformed structured output without crashing the request path.
- Prefer Chinese user-facing error messages consistent with existing `BusinessException` copy.

## Verification

After backend changes, run at least:

```bash
pnpm --filter @forgeai/backend lint
pnpm --filter @forgeai/backend typecheck
uv run --directory apps/backend python -m unittest discover -s tests -v
pnpm encoding:check
```

When migrations change, also run:

```bash
uv run --directory apps/backend alembic heads
```

Do not claim the task is done if any of these fail.
