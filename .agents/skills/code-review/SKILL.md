---
name: code-review
description: >-
  Review ForgeAI diffs, commits, branches, pull requests, or working-tree changes
  for correctness, regressions, security, and missing tests without implementing fixes.
---

# ForgeAI Code Review

Review is read-only unless the user explicitly asks for fixes.

## Establish the review scope

1. Start from the user request and the actual diff, commit, branch, or changed-file list.
2. Read the changed lines and enough surrounding code to understand their behavior.
3. Identify the direct impact surface before opening unrelated modules.

Do not perform a full-repository audit or reread every convention by default.

## Follow direct impact

Trace a change where it can realistically propagate:

- imports, callers, and shared helpers;
- API producers and consumers;
- Pydantic models, TypeScript types, serializers, and validation;
- SQLAlchemy models, migrations, queries, and existing rows;
- authentication, authorization, path, process, and preview boundaries;
- concurrency, run state, artifact provenance, and revision activation;
- tests that cover the changed behavior.

Expand the review farther only when the change affects a shared primitive or public contract,
crosses package boundaries, changes persistent data, touches security or concurrency, or reveals
evidence of a broader defect.

## Apply ForgeAI invariants when relevant

Use [architecture.md](../../../docs/architecture.md) and the
[artifact contract](../forgeai-architecture/reference.md) for the parts of the change they govern.
Check that active work has coherent inputs, outputs remain traceable, failed work cannot destroy the
last usable revision, generated code stays isolated, ownership is enforced, and contracts remain
aligned.

Do not reject an implementation merely because it differs from an old route, stage list, retry
count, agent layout, or storage path. Those details are valid only when the current code or accepted
requirement depends on them.

## Verify proportionately

Prefer the smallest command that can disprove or confirm the changed behavior. Add package-level or
repository-level checks when the impact is shared or cross-cutting. If migrations change, inspect
migration ancestry and upgrade behavior. If an API contract changes, check both sides. Never hide
checks that were not run or could not run.

## Findings

Report only actionable defects caused or exposed by the reviewed change. For each finding include:

- severity from P0 to P3;
- concise title;
- file and tight line range;
- triggering scenario;
- concrete impact;
- why the current code permits the failure.

Order findings by severity. If there are no findings, say so and note material untested areas or
remaining uncertainty.
