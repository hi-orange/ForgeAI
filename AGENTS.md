# ForgeAI — Project Agent Guide

## Product

**ForgeAI is a conversational full-stack app builder.**

The user describes an app, receives a runnable application, and keeps talking to refine it.
Generated applications currently target FastAPI, Vue, and SQLite.

## Document responsibilities

Read in this order when the files are relevant. On conflict, the higher item wins:

1. [docs/architecture.md](docs/architecture.md) — durable product direction and invariants
2. [AGENTS.md](AGENTS.md) — repository-wide working rules
3. [.agents/skills/forgeai-architecture/reference.md](.agents/skills/forgeai-architecture/reference.md) — artifact meanings and compatibility rules
4. [.agents/skills/forgeai-architecture/SKILL.md](.agents/skills/forgeai-architecture/SKILL.md) — implementation guidance

The runtime models, migrations, and tests are the source of truth for current field names and
implementation details. Package-specific rules live in
[apps/backend/AGENTS.md](apps/backend/AGENTS.md) and
[apps/frontend/AGENTS.md](apps/frontend/AGENTS.md).

## Working rules

- Implement only the current user-approved, coherent change. Do not prebuild a speculative sequence
  of future work.
- Reuse existing modules and conventions before adding new abstractions.
- Preserve run traceability, stable inputs during execution, recoverable revisions, ownership
  boundaries, and isolation of generated code.
- A user-visible behavior change must be represented in product intent before derived design and
  code claim to implement it.
- Treat architecture as stable constraints, not as a script that fixes routes, stages, agent count,
  retry budgets, or file layout forever.
- Preserve unrelated local changes.

## Review and verification

- Start with the current diff or the files changed for the task.
- Trace direct impact through callers, imports, API consumers, schemas and shared types, database
  migrations, security boundaries, concurrency behavior, and related tests.
- Expand to a wider review only when the change is cross-cutting, affects a public contract or
  shared primitive, changes persistent data, touches security or concurrency, or reveals evidence
  of a broader problem.
- Run targeted checks first. Add package-level or repository-level checks when the impact warrants
  them, and report checks that were not run.

## Before finishing

- [ ] The result matches the requested scope and architecture invariants.
- [ ] Direct consumers and affected contracts were considered.
- [ ] Relevant checks pass, or remaining failures and untested areas are stated clearly.
