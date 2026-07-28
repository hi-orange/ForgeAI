# ForgeAI

pnpm + Turborepo monorepo with a Vue frontend and FastAPI backend.

## Prerequisites

- Node.js 22.x (see `.nvmrc`)
- [pnpm](https://pnpm.io/) 9
- [uv](https://docs.astral.sh/uv/) (Python toolchain)
- Python 3.13+

## Setup

```bash
pnpm install
cd apps/backend && uv sync
```

## Develop

```bash
pnpm dev
```

- Frontend: http://localhost:5173
- Backend: http://localhost:8000 (`/health`)

## Quality gates

```bash
pnpm check   # lint + typecheck + test + format
pnpm lint
pnpm typecheck
pnpm test
pnpm format
```

## Apps

| Package             | Path            | Stack                           |
| ------------------- | --------------- | ------------------------------- |
| `@forgeai/frontend` | `apps/frontend` | Vue 3, Vite, Tailwind, Vitest   |
| `@forgeai/backend`  | `apps/backend`  | FastAPI, uv, Ruff, mypy, pytest |

Shared JS packages can go under `packages/` when needed.
