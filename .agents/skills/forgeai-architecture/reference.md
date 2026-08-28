# ForgeAI Artifact Contract

This reference defines the semantic contract between artifact producers and consumers. It does not
freeze every JSON field, Pydantic class, storage path, or executor stage. Current schemas,
migrations, and tests define the executable shape.

## Artifact responsibilities

| Artifact | Must answer |
|---|---|
| app_spec | What product behavior and user experience are intended? |
| system_design | What technical decisions implement a particular product intent? |
| code | Which exact generated source result was produced, and from which inputs? |
| test_report | What was checked against a particular code result, and what evidence was observed? |

The relationship is semantic rather than a mandatory process layout. One service may produce more
than one artifact, and orchestration may combine or split internal stages, provided responsibility
and provenance remain clear.

## Cross-artifact invariants

Every published artifact must provide enough information to establish:

- its project and semantic artifact type;
- its own version or immutable identity;
- the run or operation that produced it;
- the exact upstream artifact versions or code revision it describes;
- when it was created and which schema or compatibility version can read it.

A run must not silently switch to newer inputs after execution has begun. Consumers use the input
identities recorded for that run, not whichever artifact happens to be newest later.

Published artifact versions are not overwritten in place. A changed meaning or payload produces a
new version and preserves traceability to earlier results.

## Semantic content

### app_spec

Captures product intent: the problem being solved, user-visible behavior, data needs, interface
expectations, constraints, and acceptance signals. Its shape should fit the requested application;
it is not limited to a permanent catalog of entity, page, or field types.

### system_design

Captures implementation decisions derived from a named app_spec version. It should contain enough
information for code generation and later review, while avoiding decisions that the current
generator can derive safely and deterministically.

### code

Identifies the exact generated source result. It records relevant upstream artifact identities,
revision or workspace identity, template or generator compatibility when needed, and a verifiable
source manifest or equivalent provenance. The contract does not require a particular filesystem
layout.

### test_report

Records validation evidence for an exact code result. It distinguishes checks performed, observed
outcomes, actionable issues, and the overall conclusion. It reports; it does not silently mutate the
code result it describes.

## Schema evolution

- Use an explicit compatibility or schema version when readers need it.
- Prefer additive changes when they preserve existing consumers.
- When a breaking change is necessary, update producers, consumers, persisted data, and tests as one
  coherent change.
- Add fields because a current behavior needs them, not to predict every future workflow.
- Update this reference when artifact meaning or compatibility changes. Do not duplicate every
  current Pydantic field here.

Examples may be added for a concrete task, but they are illustrative unless the current executable
schema and tests enforce them.
