---
name: forgeai-architecture
description: Guide changes to ForgeAI orchestration, role contracts, generated-code tools and artifact lifecycles while preserving ownership, frozen inputs, isolation and recovery.
---

# ForgeAI Architecture

Implement the user's current change in the existing system. Separate durable product constraints
from mechanisms that can evolve; use runtime schemas, validators and tests for current behavior.

## Find the relevant contract

Start with the affected files and the behavior being changed. Trace their direct producers and
consumers before widening the investigation. Read the relevant sections of
[docs/architecture.md](../../../docs/architecture.md) before changing their contracts:

- Scope, approvals and ownership: core invariants and artifact lifecycle.
- Scheduling, retries and recovery: the affected scheduling and state-transition sections.
- Generated-code tools and verification: engineering execution and independent QA.
- Role instructions: prompt layering and the corresponding runtime enforcement.

Read [reference.md](reference.md) when changing artifact identity, handoffs, acceptance evidence or
compatibility. Follow existing links and inspect runtime definitions as needed. Read the full
architecture when the change spans its boundaries or the relevant constraints remain unclear.
A local change does not require loading every architectural detail.

## Preserve boundaries; improve mechanisms

- Keep execution bound to its frozen inputs and exact artifact/source identities. New versions
  preserve lineage; they do not silently replace inputs or rewrite terminal history.
- Respect artifact ownership and the separation between generated source and trusted platform/QA
  state. Code repair cannot edit frozen acceptance standards or manufacture passing evidence.
- Preserve recoverable revisions, concurrency fences and recorded work across retries. Resume or
  migration must not silently reset spent budgets or revive a terminal execution.
- Treat smoke, independent acceptance and publication as distinct evidence and transitions.

Task decomposition, stage layout, retry policy, context selection and prompt wording are mechanisms.
Improve them within the requested scope when evidence shows a problem, and update the relevant
contract and consumers together. Record new platform behavior in the relevant product documentation.
For generated applications, new product scope needs approved intent; defects within approved intent
remain repairs. The skill adds no approval step to work the user has already authorized.

Keep enforceable boundaries in runtime validation. Put decision guidance in the existing role prompts
and reuse shared policy from `app.agents.prompts.contracts`. Avoid turning one incident's workaround
into a mandatory sequence for every application.

## Verify the outcome

Use relevant available evidence, such as tool results, saved state or execution records, to establish
what happened; distinguish those facts from model explanations. Check the changed behavior and its direct consumers first,
including the relevant failure or recovery path. Broaden verification when shared contracts, persistent
state, security or concurrency are affected. Report the checks and any remaining uncertainty;
deterministic regression tests alone do not establish better real-model success rates.
