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
9. Generated applications use React, TypeScript and FastAPI. Isolated previews default to SQLite;
   deployment uses the same SQLAlchemy/Alembic contract and may switch to PostgreSQL through
   `DATABASE_URL`. Authentication, email and external services are added only when approved intent
   requires them, rather than being hidden baseline behavior.
10. Terminal runs are immutable. A post-completion product change or implementation repair creates a
    new `BuildRun` with an explicit `RunRevision` edge to the exact prior app specification and code
    baseline. Product changes return to approval; repairs retain approved intent and receive a new
    code identity and acceptance run.
11. Stop and pause are different transitions. Pause fences only the active execution and remains
    resumable. Cancel atomically closes the active execution, task, plan and BuildRun with a durable
    `cancelled` state while preserving historical artifacts.
12. Every routing outcome is a persisted, idempotent `AgentDecisionEvent`. Model turns record measured
    latency and provider token usage; configured model rates may additionally produce estimated cost.
    Eval reports distinguish metrics backed by evidence from metrics that require human labels.
13. Publication is a separate gate after acceptance. A Deployment binds one successful BuildRun to a
    provider attempt, PostgreSQL instance, secret reference, image identity, URL, bounded logs and
    rollback lineage. Preview success is never treated as publication success.

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

After a terminal run, a new message follows
`message -> classification -> RunRevision -> new BuildRun -> approval or repair -> independent gates`.
It never reopens or mutates the old run.

Any external integration, authentication or permission boundary, concurrency requirement, persisted
contract migration, or breaking public contract requires Architect. Direct engineering is allowed only
when all such flags are false and the change is a small extension of existing contracts.

## Scheduling model

Leader owns ambiguous semantic decisions. The workflow engine owns deterministic execution and hard
gates.

- Leader decides product change versus implementation repair and whether Architect is required after
  approval. It does not reinterpret free-form QA text to bypass frozen artifact ownership.
- The engine auto-continues single-path handoffs: Architect completion to Code Engineer, and published
  code to Test Engineer because acceptance is the mandatory release gate.
- Quality failure routing uses typed report state: blocked evidence without a challenge → retry
  infrastructure; failed acceptance → Code Engineer against the same frozen intent; frozen-test or
  intent conflict → explicit user challenge. A product/design change then starts a newly approved
  product lineage instead of letting Test Engineer mutate or indirectly replace system design.
- Leader outputs structured `NextAction` decisions; the engine validates approval, artifact identity,
  ownership and frozen inputs before creating tasks.
- The validated decision is persisted before side effects. Retries replay the same decision key rather
  than invoking Leader again, and the event records whether application succeeded or failed.

## Evaluation and release evidence

- Project Eval aggregates end-to-end success, acceptance results, first-attempt completion, repair
  rounds, routing-contract validity, unnecessary Leader use, trajectory completeness, replayability,
  elapsed time, tokens and configured cost from persisted records.
- Classification and routing accuracy are reported only when a human or benchmark label exists. An
  unlabelled production trace is marked unavailable, not counted as correct by construction.
- CI builds the isolated check image and runs the pinned template through database migration, real API,
  frontend build and Chromium screenshot checks. Unit mocks remain useful but do not replace this gate.
- The initial publication provider is bounded Docker Compose: generated backend/frontend images and a
  PostgreSQL service, secret material outside the generated workspace, durable status/logs, and rollback
  to the previous deployment. Additional cloud providers must implement the same persisted contract.

## Prompt layering

Role prompts are assembled in four layers: hard platform boundaries, current workflow state, role
decision policy, and optional examples. Hard gates belong in runtime validation whenever possible;
prompts explain decisions without becoming the only enforcement mechanism.
