---
name: forgeai-architecture
description: Applies ForgeAI artifact ownership, approval, frozen-input, quality-gate, recovery, and prompt-contract invariants. Use when changing ForgeAI orchestration, role prompts, generated-code tooling, artifacts, or delivery state transitions.
---

# ForgeAI Architecture

## Workflow

1. Read `docs/architecture.md` completely.
2. Read `reference.md` when changing artifacts, approvals, engineering handoff, smoke checks, acceptance testing, or repair routing.
3. Inspect current schemas, migrations, services and tests before relying on a field name.
4. Preserve frozen inputs, exact artifact identities, ownership boundaries, generated-source isolation and recoverable revisions.
5. Put hard gates in runtime validation. Keep prompts focused on role decisions and import shared policy from `app.agents.prompts.contracts` instead of copying it.
6. Run targeted tests for direct consumers, then broader package checks when a public contract changes.

## Boundaries

- Do not turn an implementation mismatch into a product change when approved intent already covers it.
- Do not use clarification as approval.
- Do not modify frozen acceptance assets from Code Engineer paths.
- Do not let smoke evidence substitute for independent acceptance evidence.
- Preserve the distinction between approved-standard `test_hash`, QA asset `suite_hash` and each
  source-bound execution id. QA adapter revisions never overwrite snapshots or weaken assertions.
- Keep generated application processes unable to write trusted QA evidence; external effects are
  disabled during independent acceptance. Check publication against persisted runner facts.
- Do not add product behavior in architecture, engineering, or testing prompts.
