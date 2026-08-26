# ForgeAI — Project Agent Guide

## Product

**ForgeAI = conversational full-stack app builder** (FastAPI + Vue + SQLite).

User talks → runnable app → keeps talking to refine. Not a static website generator.

## Document priority

Read in this order. **On conflict, higher wins:**

1. [docs/architecture.md](docs/architecture.md) — system design (single source of truth)
2. [AGENTS.md](AGENTS.md) — this file (direction + rules)
3. [.agents/skills/forgeai-architecture/reference.md](.agents/skills/forgeai-architecture/reference.md) — artifact schemas
4. [.agents/skills/forgeai-architecture/SKILL.md](.agents/skills/forgeai-architecture/SKILL.md) — how to implement
5. [docs/ROADMAP.md](docs/ROADMAP.md) — what phase we are in

Package guides: [apps/backend/AGENTS.md](apps/backend/AGENTS.md) · [apps/frontend/AGENTS.md](apps/frontend/AGENTS.md)

## Hard rules

- Artifacts: `app_spec` → `system_design` → `code` → `test_report`
- User changes: PM updates spec first, then cascade (never Dev-only for features)
- New builds use `POST /build-runs`, not legacy static `/build`
- Dev owns generate/update/repair; QA only reports issues
- Check ROADMAP phase before coding — do not skip ahead

## Before merging

- [ ] Matches [architecture.md](docs/architecture.md)?
- [ ] Reuses existing modules?
- [ ] Backend lint + typecheck + tests pass?
