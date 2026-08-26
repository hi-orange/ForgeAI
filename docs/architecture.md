# ForgeAI Architecture

**This file is the single source of truth for system design.** If other docs conflict, this wins.

Quick links: [AGENTS.md](../AGENTS.md) (direction) · [reference.md](../.agents/skills/forgeai-architecture/reference.md) (artifact schemas) · [ROADMAP.md](ROADMAP.md) (current phase)

---

## What ForgeAI is

User describes an app in natural language → ForgeAI delivers a **runnable** FastAPI + Vue + SQLite app → user keeps chatting to refine it.

Agents do not chat with each other. They read and write **structured artifacts** (like MetaGPT documents).

---

## Components

| Part | Path | Job |
|------|------|-----|
| API | `apps/backend/app/api/v1/` | HTTP: projects, build-runs, chat, preview |
| Orchestrator | `app/orchestrator/` | Run SOP, lock BuildRun, schedule agents |
| Artifact pool | `app/orchestrator/artifact_pool.py` | Store versioned JSON artifacts |
| Agents | `app/agents/` | pm, architect, developer, qa |
| Runtime | `app/runtime/` | Start/stop preview, reverse proxy |
| Tools | `app/tools/` | Safe file I/O, run server, smoke test |
| Template | `app_template/` | Fixed scaffold; Dev fills business files |
| Storage | `storage/projects/{id}/` | Workspaces, revisions, artifact files |

---

## Four agents

| Agent | Writes | Never does |
|-------|--------|------------|
| PM | `app_spec` | Code, design |
| Architect | `system_design` | Code |
| Developer | `code` (generate / update / repair) | Change spec without PM |
| QA | `test_report` | Code |

User changes **always** start with PM updating `app_spec`, then cascade down.

---

## Artifact flow

```
app_spec (PM) → system_design (Architect) → code (Developer) → test_report (QA)
```

Artifact schemas: [reference.md](../.agents/skills/forgeai-architecture/reference.md)

---

## Four design decisions (fixed)

### 1. BuildRun locks versions

Each build/update gets a `run_id`. All agents in that run read **pinned artifact versions**, not "latest".

```
BuildRun run_abc:
  input:  app_spec v2
  output: system_design v2, code rev 3, test_report for run_abc
```

If user sends a new message while a run is active, queue it or reject — do not mix versions mid-run.

### 2. Workspace → immutable revision

Code is never edited in place on the active revision.

```
storage/projects/{id}/
  runs/{run_id}/workspace/     ← Dev writes here during run
  revisions/3/                 ← promoted after QA pass (immutable)
  active.json                  ← { "revision": 3 }
```

Success: copy workspace → new revision, update `active.json`. Failure: discard workspace.

### 3. Runtime (Phase 1)

Host processes, two ports per project:

- Backend: `uvicorn` on allocated port
- Frontend: `vite preview` on allocated port
- ForgeAI proxies `/preview/{project_id}/` → frontend port

Generated code is **untrusted**. Preview runs in isolated ports; idle 10 min → stop. No arbitrary shell for agents — see Tools below.

### 4. Async builds

Long builds do not block HTTP:

```
POST /projects/{id}/build-runs  → { run_id, status: "queued" }
GET  /projects/{id}/build-runs/{run_id}  → { status, stage, error? }
```

Stages: `pm` → `architect` → `developer` → `qa` → `runtime` → `ready` | `failed`

Legacy `POST /projects/{id}/build` (static site) is **frozen**. New pipeline uses `/build-runs` only.

---

## SOP: first build

```
1. Create BuildRun (lock inputs)
2. PM      → app_spec        → publish
3. Architect → system_design → publish (reads locked app_spec)
4. Developer → workspace     → smoke self-check (≤2 fixes)
5. QA      → test_report     → if fail: Dev repair (see budget)
6. Promote workspace → revision N, set active
7. Runtime.start(revision N)
8. BuildRun status = ready
```

## SOP: user change (chat)

Same as build, but Developer mode = `update`, inputs include previous revision as base.

Always step 2 = PM updates spec first. No Dev-only patches for feature changes.

---

## Repair budget (one rule)

Per BuildRun: **at most 5 Developer LLM calls** total (generate/update + all repairs).

QA runs at most **3 times** (first check + 2 re-checks after repair). Dev self-check during generate counts toward the 5.

---

## BuildRun state

| Status | Meaning |
|--------|---------|
| `queued` | Waiting |
| `running` | Agent stage in progress (`stage` field names current agent) |
| `ready` | Revision promoted, preview up |
| `failed` | Stopped; workspace discarded; error stored |

Project status for UI: `draft` | `building` | `ready` | `failed` (maps from active BuildRun).

---

## Tools (security)

| Rule | Detail |
|------|--------|
| Scope | Current project's `runs/{run_id}/workspace/` only |
| Paths | Resolve and reject `..` escapes |
| Commands | Whitelist: start server, smoke test, read/write listed files |
| Limits | Timeout 120s, log cap 64KB, kill orphan processes on run end |

---

## Data model (target)

```
Project          id, user_id, name, prompt, status, active_revision
BuildRun         id, project_id, run_id, status, stage, locked_*_version, error
ProjectArtifact  id, project_id, type, version, run_id, content_path
ChatMessage      id, project_id, role, content, run_id?, created_at
```

Pydantic schemas live in `app/schemas/artifacts/` (Phase 1 task).

---

## API (target)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/projects` | Create |
| POST | `/projects/{id}/build-runs` | Start build SOP |
| GET | `/projects/{id}/build-runs/{run_id}` | Poll status |
| POST | `/projects/{id}/chat` | User message → update SOP |
| GET | `/projects/{id}/preview` | Proxy to running app |

Legacy (frozen): `/start`, `/approve-spec`, `/build` (static website).

---

## Legacy migration

| Old | New |
|-----|-----|
| `ProductManagerAgent` | `agents/pm.py` |
| `WebsiteBuilderAgent` | `agents/developer.py` |
| `WebsiteQualityAgent` | `agents/qa.py` |
| `SiteReviserAgent` | `developer.update` |
| `generated_files` column | `revisions/{n}/` + code artifact |
| Static iframe srcdoc | `/preview/{id}/` proxy |

Do not add features to legacy paths.

---

## Why these choices

| Choice | Reason |
|--------|--------|
| Document-driven | Spec is checkable; agents don't drift via chat |
| BuildRun pin | Prevents v2 spec + v1 design mismatches |
| Immutable revisions | Rollback, eval, audit |
| Async build-runs | Builds take minutes; don't hold HTTP |
| Template scaffold | Dev fills business only; quality stable |
