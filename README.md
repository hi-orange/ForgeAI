# ForgeAI

**Conversational full-stack app builder** — describe an app in natural language, get a runnable FastAPI + Vue + SQLite application, refine it through chat.

Monorepo: Vue frontend + FastAPI backend (pnpm + Turborepo).

## Docs

| Doc | Purpose |
|-----|---------|
| [AGENTS.md](AGENTS.md) | AI/human project guide |
| [docs/architecture.md](docs/architecture.md) | System design (source of truth) |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Current implementation phase |

## Setup

```bash
pnpm install
cd apps/backend && uv sync
pnpm dev
```

- Frontend: http://localhost:5173
- Backend: http://localhost:8000

## Quality

```bash
pnpm check
```

## Status

Migrating from legacy static-website generator to dynamic app builder. See [ROADMAP.md](docs/ROADMAP.md) — **Phase 1** in progress.
