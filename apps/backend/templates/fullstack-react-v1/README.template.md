# Generated application (fullstack-react-v1)

This workspace starts from the ForgeAI `fullstack-react-v1` template: React + TypeScript +
TanStack Router/Query + Tailwind CSS + FastAPI + SQLite. Its structure is intentionally aligned
with the official FastAPI full-stack template while remaining small enough for isolated previews.

Business features (for example jobs or applications) are **not** included here. They are added later from an approved product plan.

## Prerequisites

- Python >= 3.13
- Node.js >= 22.18 and < 23
- `uv` (backend) and `npm` (frontend)

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
npm install
npm run dev
```

Dev server proxies `/api` to `http://localhost:8000`.

The frontend includes a typed request boundary in `src/lib/api.ts`. After the backend is running,
you can also save its OpenAPI document as `frontend/openapi.json` and run `npm run generate-client`
to generate a client into `src/client`.

## End-to-end acceptance

`forgeai.smoke.json` is part of the application contract. Keep it current whenever a feature adds
or changes an API, route, form, or important button. It must exercise real business endpoints and
browser actions; once the app has business features, a health-only manifest is not sufficient.
Write operations should be followed by a read that proves the result was persisted.

The isolated `all` check starts FastAPI and the Vite dev server, runs the declared API requests,
then uses Chromium to verify the pages actually call those APIs and complete the key interactions.

Its `visual_contract` declares the real theme entry, required semantic color tokens, repository-
local visual assets and the approved theme intent. Use `custom` when creating a domain palette and
`preserve` when the user explicitly wants the established palette. The isolated check only
requires palette differences for `custom`; feature-only iterations keep the established theme.

## Visual baseline

Derive a domain-specific palette, typography hierarchy and asset treatment for each product. Keep
at least a primary, an accent and a neutral family, and use repository-local SVG/CSS illustrations
or legitimate local assets when imagery supports the experience. Do not ship the generic template
copy or rely on remote image hotlinks.

## Notes

- Do not commit `.env`, `*.db`, `node_modules`, or `.venv`.
- Keep generated secrets out of source control.
