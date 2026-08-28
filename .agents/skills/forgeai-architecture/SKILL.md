---
name: forgeai-architecture
description: >-
  Implement or change ForgeAI build runs, artifacts, orchestration, generation,
  runtime, tools, or generated-app templates.
---

# ForgeAI Architecture Work

## Read the relevant sources

Before changing architecture-sensitive code, read:

1. [architecture.md](../../../docs/architecture.md) for durable product direction and invariants.
2. [reference.md](reference.md) when artifact meaning or compatibility is affected.
3. The current models, schemas, migrations, services, and tests for present implementation details.

The architecture is a constraint set, not a fixed execution script.

## Work from the current request

- Implement only the current user-approved, coherent increment.
- Reuse existing ownership boundaries and modules before adding another abstraction.
- Derive routes, states, stages, storage paths, agent roles, and retry behavior from the current code.
- Do not prebuild later work or encode a speculative project sequence in documentation.
- When the current design needs to change, preserve the invariant first and choose the simplest
  implementation that satisfies the request.

## Preserve these properties when they are in scope

- A long-running operation is observable and traceable to its inputs, outputs, errors, and result.
- Active work does not silently mix input versions.
- Artifacts identify their semantic inputs and the run that produced them.
- Product behavior changes are represented in product intent before derived outputs claim them.
- A failed generation attempt does not destroy the last usable revision.
- Generated files, commands, processes, and previews remain inside their authorized boundaries.
- Ownership, API contracts, database migrations, and frontend consumers stay coherent.

The exact number of agents, order of internal stages, model-call budget, schema fields, and physical
storage layout may evolve.

## Assess impact before editing

Name the existing files or modules the change will reuse or affect. Start review from the changed
files and trace only direct callers, imports, contracts, persistence, runtime boundaries, and tests.
Expand farther when a shared primitive, public contract, data migration, security boundary,
concurrency behavior, or concrete evidence makes the impact broader.

Do not scan or re-audit the entire repository by default.

## Verify proportionately

Run focused tests for the changed behavior first. Add linting, type checking, package suites,
migration checks, frontend checks, or repository-wide checks when the affected surface warrants
them. Record what ran and what relevant checks did not run.

Before finishing, confirm that the result matches architecture invariants, keeps artifact and
revision provenance intact where applicable, and does not include unrelated future work.
