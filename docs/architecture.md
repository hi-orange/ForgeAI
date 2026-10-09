# ForgeAI Architecture

ForgeAI is a conversational full-stack application builder. A user-approved product intent is
turned into an isolated, traceable and recoverable generated application.

## Durable invariants

1. Product intent is versioned. A user-visible behavior change starts from a new `app_spec`; an
   implementation that does not match an already-approved behavior is a repair, not a product change.
2. Approval and clarification are separate. Clarification collects missing information. Approval is
   an explicit persisted UI/API transition from `awaiting_approval` to delivery.
3. Every execution uses frozen inputs. Later messages and newer artifacts never replace inputs of an
   in-flight task.
4. Artifact ownership is enforced. Product Manager owns `app_spec`, Architect owns `system_design`,
   Code Engineer owns generated source, and Test Engineer owns frozen acceptance evidence.
5. Generated source is isolated from ForgeAI platform source. Writes are optimistic-concurrency
   protected, revisions are recoverable, and quality checks run against an exact source identity.
6. `forgeai.smoke.json` is the mandatory engineering integration gate. A frozen acceptance plan is
   the independent final quality gate. Neither substitutes for the other.
7. A frozen test that conflicts with approved intent is challenged, not edited. Resolution either
   confirms the test and repairs code, or creates a new approved product version and a new test hash.
8. Cross-role policy has one source of truth. Runtime schemas and validators define exact fields;
   `app.agents.prompts.contracts` defines compact policy fragments injected into role prompts.

## Rule priority

1. Security, permissions, ownership, isolation and immutable artifact identity.
2. Explicitly approved user intent and negative constraints.
3. The frozen acceptance contract bound to that intent; conflict enters challenge resolution.
4. Data integrity, concurrency protection, recoverability and engineering correctness.
5. Execution efficiency.
6. Default visual and style guidance.

## Delivery state transitions

`message -> classification -> app_spec draft -> awaiting_approval -> approved app_spec -> architecture
decision -> code candidate -> smoke gate -> immutable code artifact -> acceptance gate -> release or
bounded repair`

Any external integration, authentication or permission boundary, concurrency requirement, persisted
contract migration, or breaking public contract requires Architect. Direct engineering is allowed only
when all such flags are false and the change is a small extension of existing contracts.

## Prompt layering

Role prompts are assembled in four layers: hard platform boundaries, current workflow state, role
decision policy, and optional examples. Hard gates belong in runtime validation whenever possible;
prompts explain decisions without becoming the only enforcement mechanism.
