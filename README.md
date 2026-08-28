# ForgeAI

**Conversational full-stack app builder** — describe an app in natural language, get a runnable FastAPI + Vue + SQLite application, refine it through chat.

Monorepo: Vue frontend + FastAPI backend (pnpm + Turborepo).

## Docs

| Doc | Purpose |
|-----|---------|
| [AGENTS.md](AGENTS.md) | AI/human project guide |
| [docs/architecture.md](docs/architecture.md) | System design (source of truth) |

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

## Development

[architecture.md](docs/architecture.md) records the durable product direction and safety
constraints. Implementation details evolve through small, verified changes instead of a fixed
end-to-end sequence. See [AGENTS.md](AGENTS.md) for contribution and review scope.
