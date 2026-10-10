# Generated application (fullstack-react-v1)

This workspace starts from the ForgeAI `fullstack-react-v1` template: React + TypeScript +
TanStack Router/Query + Tailwind CSS + FastAPI + SQLAlchemy/Alembic. It was refreshed from the
official FastAPI full-stack template at commit `7257e606cb94eada1dca40173e2b7f99dd549bc6`, then
trimmed for generated applications: SQLite stays the zero-configuration preview database and the
same model/migration layer supports PostgreSQL for deployment through `DATABASE_URL`.

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

For PostgreSQL, set `DATABASE_URL=postgresql+psycopg://...` before running migrations. The root
`compose.yml` is a production-oriented starting point with PostgreSQL, backend migration/startup,
and a frontend reverse proxy.

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

`frontend/public/assets/images/` is the stable destination for generated or curated image assets.
Do not remove this directory when a project does not yet use images; ForgeAI image tasks write
there and frontend code should reference those files with root-relative `/assets/images/...` URLs.

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
- Authentication, email and third-party services are intentionally not baseline behavior. Add them
  only when the approved product requirements need them; the official upstream remains the reference
  for those optional production capabilities.
