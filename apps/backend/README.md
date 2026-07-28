# @forgeai/backend

FastAPI service managed with [uv](https://docs.astral.sh/uv/).

## Setup

```bash
uv sync
```

## Scripts (via pnpm from repo root)

```bash
pnpm --filter @forgeai/backend dev
pnpm --filter @forgeai/backend lint
pnpm --filter @forgeai/backend typecheck
pnpm --filter @forgeai/backend test
```

Or from this directory:

```bash
uv run uvicorn app.main:app --reload --port 8000
uv run ruff check app tests
uv run mypy app
uv run pytest
```

Health check: `GET /health`
