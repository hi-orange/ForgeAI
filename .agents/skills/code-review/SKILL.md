---
name: code-review
description: Review ForgeAI code changes for correctness, regressions, security, and missing tests. Use when asked to review a diff, branch, commit, PR, or current working tree; do not implement fixes unless explicitly requested.
---

# ForgeAI Code Review

Review the requested change and report actionable findings before any summary. Treat review as
read-only unless the user separately asks for fixes.

## Establish scope

- Determine whether the target is the working tree, staged changes, a commit range, or a named
  branch/PR. If the request does not name a target, review the current working tree against `HEAD`.
- Inspect repository instructions and the surrounding code needed to understand changed behavior.
- Preserve unrelated user changes and do not infer authorization to commit, push, or modify files.

## Review priorities

Prioritize concrete defects over style preferences:

1. Broken behavior, data loss, security boundaries, authorization, or unsafe generated content.
2. Incorrect state transitions across the Product Manager, approval, Builder, validation, repair,
   persistence, and preview workflow.
3. Frontend/backend contract mismatches, stale project state, races, and error details that are lost
   before reaching the UI.
4. Generated-site isolation issues: iframe sandboxing, untrusted HTML/JS, form submission, network
   access, duplicate selectors, and preview behavior that differs from saved output.
5. Database model/schema/migration inconsistencies, invalid Alembic ancestry, and upgrade behavior for
   existing rows.
6. Chinese text corruption, non-UTF-8 files, unsafe JSON parsing, and DeepSeek response truncation or
   malformed structured output.
7. Missing tests for changed behavior, especially repair loops, validation rules, project status
   transitions, and interactive preview behavior.

Only report an issue when the changed code demonstrably causes it or leaves a meaningful regression
risk. Do not require production infrastructure from the static three-file website prototype unless
the approved specification explicitly includes that capability and the platform supports it.

## Verification

Use focused read-only checks first. When dependencies are available, run checks proportional to the
change:

- `pnpm check` for the full UTF-8, lint, type, and formatting suite.
- `uv run --directory apps/backend python -m unittest discover -s tests -v` for backend behavior.
- `uv run --directory apps/backend alembic heads` when migrations change.
- `git diff --check` for patch hygiene.

Do not call paid model APIs or mutate the database merely to perform a review. If a relevant check
cannot run, state that limitation instead of guessing that it passed.

## Findings format

List findings from highest to lowest severity. Each finding must include:

- severity (`P0` critical, `P1` high, `P2` medium, or `P3` low);
- a short defect-focused title;
- the tightest useful file and line reference;
- the triggering scenario and observable impact;
- why the existing code does not prevent it.

Keep summaries brief. If no actionable defects are found, say so explicitly and mention any important
untested areas or verification limitations. Avoid praise, generic best practices, and large code
rewrites in the review response.
