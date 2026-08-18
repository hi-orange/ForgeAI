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
```

Or from this directory:

```bash
uv run uvicorn app.main:app --reload --port 8000
uv run ruff check app
uv run mypy app
```

## Logs

Application logs are written to the terminal and to `logs/forgeai.log` in UTF-8.
The file rotates at 10 MB and keeps five backups by default. These values can be
changed with `LOG_LEVEL`, `LOG_FILE`, `LOG_MAX_BYTES`, and `LOG_BACKUP_COUNT`.
