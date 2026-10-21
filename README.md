# ForgeAI

**Conversational full-stack app builder** — describe an app in natural language, get a runnable React + FastAPI application, refine it through chat, and deploy it with SQLite for local previews or PostgreSQL in production.

Monorepo: Vue frontend + FastAPI backend (pnpm + Turborepo).

## Build progress

The conversation presents build progress as short descriptions of meaningful work, with one
representative file or tool action per group and the rest behind “show more”. The latest phase is
expanded initially; polling respects the user's collapse choices. Internal decision calls and repeated
retries do not create separate progress steps. Pauses and failures that require attention remain
visible, and diagnostic evidence remains available in expanded details and the console.

The source browser also exposes read-only `forgeai/REQUIREMENTS.md`, `forgeai/ARCHITECTURE.md`
and `forgeai/PROGRESS.md` as their saved artifacts become available, including before the first
code preview. These documents describe the current run's actual task inputs and published results,
with artifact version and provenance. They render as readable documents and do not alter generated
source, source hashes, or approval state. An unfinished design is not presented as a saved design.
Progress includes the accepted engineering graph, dependencies and actual check outcomes; engineering
completion and independent acceptance are distinct. Documents follow the exact run/attempt and refresh
after material changes while keeping the user's selected file. Failure, repair and recheck remain
separate groups, and batched source writes refresh the editor just like individual edits.

At engineering startup the platform supplies a bounded pack of current workspace entry points and
contracts with file hashes. This avoids model-driven template discovery while preserving targeted
reads for omitted or changed files. The engineer can explicitly adopt a validated initial dependency
graph by its displayed hash, or submit its own graph. Existing plans still require a complete replan.

When an implementation plan is rejected, automatic correction compares the current conflicts with
recent failed attempts, including changes that reintroduced an earlier error. Feedback distinguishes
prerequisite capabilities from user-facing feature coverage. Corrections preserve approved scope,
completed work and verification gates, and remain within the existing execution budget.
An independent bounded attempt history survives read-heavy investigation and recovery; successful
plan acceptance starts a new history boundary without deleting audit records.

The base template keeps authentication optional. A frozen architecture can select the versioned
`auth-session-v1` capability for approved identity requirements; its source digest is pinned for that
execution. The engineer installs it through the same staged, fenced and recoverable source-write
path. It supplies password login, expiring/revocable sessions, current identity, a database migration
and React auth state. It creates no default account or public registration route; approved user
provisioning, UI and business record ownership still need implementation and verification. Simple
sites select no capability and receive no auth files or auth-install tool. Custom entry points or
migration chains require deliberate integration rather than being overwritten by the installer.

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
