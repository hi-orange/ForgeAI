# ForgeAI Architecture

**This file is the source of truth for ForgeAI product direction and cross-module invariants.**

It intentionally does not prescribe a permanent delivery sequence or orchestration script, an
exhaustive API list, or a final directory layout. The code, schemas, migrations, and tests define
the current implementation. When an implementation detail changes without changing the product
direction or an invariant below, this document does not need to be expanded into a step-by-step
plan.

Quick links: [project guide](../AGENTS.md) ·
[artifact contract](../.agents/skills/forgeai-architecture/reference.md)

## Product goal

ForgeAI is a conversational full-stack app builder.

A user describes an application, receives a runnable app, and continues the conversation to refine
its behavior and interface. Generated applications currently target FastAPI, Vue, and SQLite, while
the generation strategy and internal implementation may evolve.

## Requirement approval experience

The project workspace keeps conversation on the left and the application preview on the right.
After the user requests a build, propose a short, actionable feature checklist. Users can select,
edit, or add requirements and approve the plan in one step. Use reasonable defaults for ordinary
product details; ask questions only when the application goal cannot be understood. A proposal
must wait for explicit approval before downstream work starts. Persist the approved selection as
a new product-intent version, with traceability to its proposal.

Approval preserves acceptance clauses only for unchanged selected features. Edited, added, or
uncovered features require an explicit observable acceptance condition in the checklist before
dispatch; approval must not invent one or carry over contradictory proposal text. Legacy v1
requirements are read through a stable in-memory adapter; new approvals are v2 artifacts linked to
the unchanged original. An old automatic design assignment does not substitute for user approval.

## Progressive engineering experience

After approval, engineering proceeds through small changes in an isolated workspace. The agent
reads files, implements database migrations, backend APIs and frontend consumers, and receives
real execution results to repair failures. The conversation shows ordered work summaries and
tool starts, results and errors; refreshing preserves this history. File changes refresh the
source viewer. The workspace template is browsable as soon as it exists, clearly distinguished
from business code. Build activity is grouped into collapsible steps with a compact current-step
view; internal model calls do not flood the conversation. Reading earlier steps must not force
the conversation back to the bottom. Successful writes update the source viewer from real files.
A completion request must run platform-controlled engineering checks against the
current source before a work item can be marked checked. Passing migration, startup and build
checks is not a claim of full business acceptance or a published, previewable revision.

Generated code runs only in a restricted execution environment. An unavailable environment pauses
work with an actionable reason. Explicit continuation retains frozen inputs, files and history,
starts a new execution attempt, and preserves the previous attempt's usage and outcome.

## Leader-directed delivery

Leader is the only role that interprets a user turn as work, creates the next assignment, and
decides whether a completed result should be followed by another role. Product Manager,
Architect, Code Engineer, and Test Engineer execute assigned work and publish results; they do not
create downstream assignments themselves. Non-product conversation is answered without starting a
delivery run.

Every new or changed product behavior still passes through a user-approved product-intent version.
After approval, Leader may send a small conventional application directly to Code Engineer using
the fixed platform stack, or require Architect to publish a system design first when scope or
constraints warrant it. The chosen path is pinned to the exact approved artifact and remains
observable in the task and execution inputs.

## Core concepts

| Concept | Responsibility |
|---|---|
| Project | Owns the conversation, generated application, and user-visible state |
| BuildRun | Gives a long-running build or update a traceable identity and observable outcome |
| Artifact | Records versioned intent, design, code identity, or validation evidence |
| Revision | Represents a recoverable version of generated source code |
| Runtime | Starts and exposes a generated revision for preview or use |

These responsibilities are stable. Their class names, database fields, modules, and storage
locations are implementation details.

## Architecture invariants

### Work is observable and traceable

- Long-running generation and update work has a durable run identity.
- A run exposes enough state to understand whether it is waiting, active, successful, or failed.
- Inputs, outputs, errors, and the resulting revision can be traced back to that run.
- Concurrent requests for the same project have deterministic behavior; they must not silently
  corrupt or mix work.

### Inputs and outputs do not drift during a run

- Once execution begins, the run uses explicitly identified input versions.
- Published outputs record which inputs produced them.
- A newer conversation message cannot silently replace an input already being used by active work.

The exact point at which inputs are pinned and the way pending requests are queued or rejected may
change with the implementation.

### Artifacts preserve responsibility boundaries

ForgeAI currently uses four semantic artifact types:

| Artifact | Meaning |
|---|---|
| app_spec | Product intent and user-visible behavior |
| system_design | Technical decisions derived from a specific product intent |
| code | Identity and provenance of generated source |
| test_report | Validation evidence for a specific code result |

Their dependency is semantic: product intent informs design, design informs code, and validation
describes a code result. This does not require a permanent number of agents, processes, model calls,
or executor stages.

A requested product behavior change must be reflected in product intent before downstream outputs
claim to implement it. A repair that only makes existing intent work does not need to invent a new
product requirement.

### Revisions remain recoverable

- Active generated source is not edited destructively in place.
- Work is isolated until it is suitable to become a new revision.
- Published revisions retain their identity and provenance.
- A failed attempt does not destroy the last usable revision.

The promotion mechanism and physical storage strategy may change while preserving these properties.

### Generated code is untrusted

- File access, commands, processes, network access, and preview exposure are restricted to the
  intended project and operation.
- Paths are resolved and checked before access.
- Runtime resources are bounded and can be stopped or cleaned up.
- Generated applications do not inherit ForgeAI platform privileges by default.

### Ownership and contracts stay coherent

- Project and run operations enforce the existing user ownership boundary.
- Public contract changes are updated across producers, consumers, types, and tests.
- Persistent data changes use explicit migrations and preserve a valid upgrade path.

## Details that may evolve

The following are deliberately not frozen here:

- API routes and payload fields
- database columns and status or stage names
- the number, names, prompts, and ordering of agents or services
- retry limits, model-call budgets, and scheduling strategy
- artifact serialization and storage layout
- generated app templates and runtime or isolation technology
- package, module, and file organization

Choose these details from the current requirement and existing code. Update this document only when
the product direction, a responsibility boundary, or an invariant changes. Avoid documenting
unimplemented future work as though it were already decided.

## Delivery approach

Work in small, user-approved increments. Reuse the current implementation, make the smallest
coherent change, verify the behavior affected by that change, and decide later work after the
current result is understood. ForgeAI does not use a permanently fixed phase-by-phase project plan.

## Impact-based review

Review starts from the current change set, not from an automatic scan of the entire repository.
Trace the changed code into the places it can directly affect, such as callers, imports, API
consumers, schemas, migrations, shared types, security boundaries, concurrency behavior, and tests.

Expand the review only when the change touches a shared primitive or public contract, crosses
packages, changes persistent data, affects security or concurrency, or when evidence points to a
broader issue. Verification follows the same rule: targeted checks first, broader suites when the
impact justifies them.
