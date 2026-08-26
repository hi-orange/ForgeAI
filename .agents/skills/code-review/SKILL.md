---
name: code-review
description: Review ForgeAI code changes for correctness, regressions, security, and missing tests. Use when asked to review a diff, branch, commit, PR, or current working tree; do not implement fixes unless explicitly requested.
---

# ForgeAI Code Review

Read-only unless user asks for fixes.

## Scope

Identify whether changes are **legacy** (static website) or **target** (BuildRun, artifacts, runtime). Apply matching criteria.

Read [docs/architecture.md](../../../docs/architecture.md) for target design.

## Target code — check for

1. BuildRun pins artifact versions (no "read latest" mid-run)
2. Workspace → revision promote (no in-place overwrite of active revision)
3. User changes cascade from PM spec update
4. Repair budget respected (≤5 Dev calls, ≤3 QA runs per run)
5. Tool sandbox: path scope, no shell escape, whitelist commands
6. Preview isolation for untrusted generated code
7. `/build-runs` vs legacy `/build` not mixed in same handler
8. Pydantic artifact schemas match [reference.md](../forgeai-architecture/reference.md)

## Legacy code — check for

1. Do not break existing static flow until migration completes
2. Status transitions: draft → prd → approve → build → completed
3. `generated_files` JSON integrity, iframe preview isolation
4. Do not require BuildRun infrastructure from legacy-only changes

## Always check

- Auth boundaries, UTF-8, defensive JSON parsing
- Frontend/backend contract alignment
- Tests for changed behavior
- Alembic single-head if migrations touched

## Verify (read-only)

```bash
pnpm check
uv run --directory apps/backend python -m unittest discover -s tests -v
```

## Findings format

Severity (P0–P3), title, file:line, scenario, impact, why current code fails.

If no defects: say so + note untested areas.
