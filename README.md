# ForgeAI

**Conversational full-stack app builder** — describe an app in natural language, get a runnable React + FastAPI application, refine it through chat, and deploy it with SQLite for local previews or PostgreSQL in production.

Monorepo: Vue frontend + FastAPI backend (pnpm + Turborepo).

## Docs

| Doc                                          | Purpose                         |
| -------------------------------------------- | ------------------------------- |
| [AGENTS.md](AGENTS.md)                       | AI/human project guide          |
| [docs/architecture.md](docs/architecture.md) | System design (source of truth) |

## Setup

```bash
pnpm install
cd apps/backend && uv sync
pnpm dev
```

The backend development task applies pending Alembic migrations before starting Uvicorn.

- Frontend: http://localhost:5173
- Backend: http://localhost:8000

Development embeds a database-backed workflow worker in the API process. For production, set
`WORKFLOW_EMBEDDED_WORKER=false` and run one or more standalone workers:

```bash
cd apps/backend
pnpm worker
```

Jobs, renewable leases, recovery attempts, and append-only workflow events use the same application
database, so an API or worker restart does not lose active work.

Published generated source is stored under `SOURCE_ARTIFACT_ROOT` (default `artifact-data`) as
content-addressed immutable snapshots. In production this path must be a durable volume shared by the
API and workers and backed up independently from disposable `RUNTIME_DATA_ROOT` workspaces.
Legacy schema-v1 code results are lazily backfilled while their workspace still exists; a legacy
workspace lost before its first backfill cannot be reconstructed because its source bytes never
existed outside that workspace.

## Quality

```bash
pnpm check
```

## Development

[architecture.md](docs/architecture.md) records the durable product direction and safety
constraints. Implementation details evolve through small, verified changes instead of a fixed
end-to-end sequence. See [AGENTS.md](AGENTS.md) for contribution and review scope.

Validate the fixed benchmark contract and score captured observations:

```bash
cd apps/backend
uv run python -m app.benchmarks --suite ../../benchmarks/forgeai-v1.json \
  --observations ../../benchmarks/fixtures/forgeai-v1-observations.json \
  --output ../../benchmark-report.json
```

The included golden observations prove the scorer contract only. Real benchmark runs must preserve
their suite version, trajectory identity, model/runtime metadata, latency, token usage and cost.
