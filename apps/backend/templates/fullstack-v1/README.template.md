# Generated application (fullstack-v1)

This workspace starts from the ForgeAI `fullstack-v1` template: Vue + FastAPI + SQLite.

Business features (for example jobs or applications) are **not** included here. They are added later from an approved product plan.

## Prerequisites

- Python >= 3.13
- Node.js >= 22.18 and < 23
- `uv` (backend) and `pnpm` or `npm` (frontend)

## Backend

```bash
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
```

Health check: `GET http://localhost:8000/api/v1/health`

SQLite database file: `backend/data/app.db` (created on first migrate/start; not committed).

## Frontend

```bash
cd frontend
pnpm install
pnpm dev
```

Dev server proxies `/api` to `http://localhost:8000`.

## Notes

- Do not commit `.env`, `*.db`, `node_modules`, or `.venv`.
- Keep generated secrets out of source control.
