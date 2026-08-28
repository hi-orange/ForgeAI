# ForgeAI Backend Agent Guide

> Architecture: [docs/architecture.md](../../docs/architecture.md)
> Repository rules: [AGENTS.md](../../AGENTS.md)
> Orchestration guidance: [.agents/skills/forgeai-architecture/SKILL.md](../../.agents/skills/forgeai-architecture/SKILL.md)

These rules apply under apps/backend/.

## Stack and conventions

- Use Python 3.13+, FastAPI, SQLAlchemy, Pydantic, Alembic, and uv.
- Prefer type annotations at public functions and service boundaries.
- Use the shared API response helpers and application exception hierarchy unless a boundary has a
  documented reason to behave differently.
- Keep LLM prompt text in the existing prompt modules instead of embedding long prompts in routers.
- Parse model output defensively and keep source, migrations, and logs in UTF-8.

## Reuse and responsibility

- Search the relevant package for an existing service, schema, model, exception, or test before
  adding another owner for the same behavior.
- Keep HTTP handling, domain behavior, persistence, orchestration, and runtime concerns in their
  existing layers.
- Prefer extending a current abstraction over copying and renaming it.
- Keep frontend and backend contracts aligned when an API shape changes.
- Treat generated source and processes as untrusted at file, command, and preview boundaries.

## Data changes

- Persistent schema changes require an Alembic revision.
- Keep migration ancestry valid and provide a safe upgrade path for existing rows.
- Prefer additive changes and explicit backfills when they reduce migration risk.
- Do not call paid model APIs or mutate a developer database merely to probe an implementation
  unless the user asks.

## Change discipline

- State which existing modules the change will reuse or affect.
- Implement the current requested increment without rewriting unrelated services.
- Preserve unrelated local changes and existing user behavior outside the request.
- Derive current routes, states, fields, and orchestration details from the code and tests; do not
  assume documentation freezes them permanently.

## Impact-based review

Start with changed backend files. Follow their direct imports, callers, routes, services, schemas,
models, migrations, frontend consumers, and tests. Expand farther only for a shared abstraction,
public contract, persistent-data change, security boundary, concurrency behavior, or evidence of a
broader regression. A routine backend change does not require a fresh audit of every backend file.

## Verification

Choose checks in proportion to the change:

- Run focused tests for the changed behavior first.
- Run backend lint and type checking when Python code is affected.
- Run the broader backend test suite for shared or cross-cutting behavior.
- Check Alembic heads when migrations change.
- Run the encoding check when text-producing or encoding-sensitive files change.

Common commands:

    pnpm --filter @forgeai/backend lint
    pnpm --filter @forgeai/backend typecheck
    uv run --directory apps/backend python -m unittest discover -s tests -v
    uv run --directory apps/backend alembic heads
    pnpm encoding:check

Report which checks ran, their results, and any relevant checks that were intentionally not run.
