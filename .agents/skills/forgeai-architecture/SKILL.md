---
name: forgeai-architecture
description: >-
  Implement ForgeAI build pipeline, BuildRun, artifact pool, agents, runtime, or
  app_template. Use when changing orchestrator, agents, runtime, tools, or migrating
  from legacy static website builder.
---

# ForgeAI Implementation Skill

## Read first

1. [docs/architecture.md](../../../docs/architecture.md) — **design truth**
2. [docs/ROADMAP.md](../../../docs/ROADMAP.md) — current phase
3. [reference.md](reference.md) — artifact contracts

Do not duplicate architecture here. This file is the **checklist**.

## Implementing a BuildRun

- [ ] Create `run_id`; reject if project already has active run
- [ ] Pin input artifact versions on the run record
- [ ] PM → publish `app_spec` (new version)
- [ ] Architect → read **pinned** spec → publish `system_design`
- [ ] Developer → write to `runs/{run_id}/workspace/` only
- [ ] Developer self-check ≤2 (counts toward 5-call budget)
- [ ] QA → publish `test_report`; re-run ≤3 total
- [ ] On pass: promote workspace → `revisions/{n}/`, update `active.json`
- [ ] On fail: discard workspace, set run `failed`
- [ ] Runtime start from active revision

## Implementing chat update

- [ ] PM updates spec first (always)
- [ ] Same cascade as build; Developer mode = `update`
- [ ] Base code from `active` revision

## Do not

- Read "latest" artifact mid-run — use pinned versions
- Overwrite `revisions/` — always promote new revision
- Let Dev change features without PM spec update
- Extend legacy `/build`, `/approve-spec`, static `generated_files`
- Give agents unrestricted shell

## Verify

```bash
pnpm --filter @forgeai/backend lint
pnpm --filter @forgeai/backend typecheck
uv run --directory apps/backend python -m unittest discover -s tests -v
```
