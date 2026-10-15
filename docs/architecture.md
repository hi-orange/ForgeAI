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
   Code Engineer owns generated source, and Test Engineer owns independent QA scenario/script drafts
   and evidence. The platform validates and freezes every test version and execution snapshot.
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
11. Stop and pause are different transitions. Pause prevents new job claims for the BuildRun,
    releases any current job lease, fences the active execution, and preserves queued work and
    checkpoints for explicit resume. Cancel atomically closes the active execution, task, plan and
    BuildRun with a durable `cancelled` state while preserving historical artifacts.
12. Every routing outcome is a persisted, idempotent `AgentDecisionEvent`. Model turns record measured
    latency and provider token usage; configured model rates may additionally produce estimated cost.
    Eval reports distinguish metrics backed by evidence from metrics that require human labels.
13. Publication is a separate gate after acceptance. A Deployment binds one successful BuildRun to a
    provider attempt, PostgreSQL instance, secret reference, image identity, URL, bounded logs and
    rollback lineage. Preview success is never treated as publication success.
14. Long-running role and deployment work is represented by a durable `WorkflowJob`, not by an
    in-process background thread. A worker claims one job with a renewable lease; a lost lease fences
    the old `TaskExecution`, preserves its checkpoint, and creates a new recovery attempt.
15. Workflow history is an append-only `WorkflowEvent` stream. Mutable task checkpoints remain useful
    for resuming tools, while UI timelines and audit/replay evidence consume immutable activity, job
    and decision facts with stable idempotency keys.
16. A published code result includes an immutable `SourceArtifact` pointer to a content-addressed,
    integrity-checked source snapshot stored outside disposable `runtime-data`. Test, preview,
    deployment and revision handoffs may rehydrate a missing workspace only from that exact
    `code_item_id` and `source_hash`; an existing mismatched workspace is never overwritten silently.
17. Independent security scanning is a sibling of acceptance, not a Test Engineer prompt convention.
    An immutable `SecurityReport` binds the exact code item, source hash and scanner version. A
    current passing report is required before deployment.

## Rule priority

1. Security, permissions, ownership, isolation and immutable artifact identity.
2. Explicitly approved user intent and negative constraints.
3. The frozen acceptance contract bound to that intent; conflict enters challenge resolution.
4. Data integrity, concurrency protection, recoverability and engineering correctness.
5. Execution efficiency.
6. Default visual and style guidance.

## Delivery state transitions

`message -> classification -> app_spec draft -> awaiting_approval -> approved app_spec -> architecture
decision -> code candidate -> smoke gate -> durable source snapshot -> immutable code artifact ->
acceptance gate -> release or bounded repair`

After a terminal run, a new message follows
`message -> classification -> RunRevision -> new BuildRun -> approval or repair -> independent gates`.
It never reopens or mutates the old run.

Any external integration, authentication or permission boundary, concurrency requirement, persisted
contract migration, or breaking public contract requires Architect. Direct engineering is allowed only
when all such flags are false and the change is a small extension of existing contracts.

## Scheduling model

### Role decisions and engineering kernel

Role names denote ownership and decision boundaries, not four interchangeable chat personas:

- Product Manager converges user intent into scope, non-goals, open questions and observable
  acceptance. New submissions must explicitly link acceptance criteria to their feature ids;
  legacy artifact readers may remain permissive. Only the user approves product intent.
- Architect resolves cross-layer contracts, dependency direction, data ownership and material
  technical tradeoffs. It reads the frozen intent before submitting design, and does not prescribe
  the engineer's tool sequence or modify application source.
- Code Engineer selects its next tool using frozen intent, current source and execution feedback.
  It reads, creates or locally edits source, runs checks and repairs from evidence. There is no
  mandatory model-generated file plan and no nested whole-file writer model. The platform stages
  mutations, fences commits by active execution and original bytes, retains recoverable revisions,
  bounds cost/no-progress, and verifies completion requests. The final source still needs independent QA.
- Test Engineer designs scenarios from approved acceptance, adapts bounded API/browser scripts to
  observed interfaces, executes them and judges the exact source/test snapshot. It separates product
  defects, script errors, unavailable infrastructure, missing evidence and contract challenges.
  Application source is read-only. QA cannot redefine acceptance to make a run pass.

### Independent QA lifecycle

The platform-owned `acceptance_test_plan` freezes approved standards and stable identities before
engineering; its `test_hash` is distinct from the versioned QA asset `suite_hash`. QA submits scenarios
with purpose, prerequisites, data, steps, expected observations, category, environment and exact
approved acceptance text. The platform checks criterion/feature coverage and preserves earlier
scenarios. Execution adapters use a bounded JSON test language, not arbitrary Python/JavaScript or
shell. API assertions and browser interactions run in a disposable off-network application with
isolated SQLite data. The application runs under a different OS user from the trusted controller and
cannot write its evidence. Required dependencies must already exist in the selected check image;
an unavailable dependency is an environment block, never a product pass.

Each execution binds `code_item_id`, `source_hash`, `suite_hash`, runner version and a unique execution
id. Operation/expectation/observation records produce case statuses; neither model-authored requirement
conclusions, smoke nor screenshots can create case passes. Every required case must pass the current
snapshot, the supplemental smoke/visual gates must pass, and no recorded defect may remain before
delivery. The publication service revalidates persisted platform observations under the execution
lease, independently of model report submission.

QA assets and execution history are versioned JSON in the existing fenced task checkpoint, and the
final report embeds its frozen suite for durable repair handoff. Local execution files are diagnostic
copies, not the recovery authority. Adapter revisions preserve scenarios, input/action order and
assertions; changing expected behavior or assertions enters explicit challenge resolution. Product
repair inherits the original suite and reruns all scenarios against the new source; old observations
cannot be restored into the new identity. Recovery preserves tool, revision and execution budgets.
Legacy plans/reports remain readable. New report publication cannot use legacy implicit synthesis
to satisfy independent evidence requirements.

The current engineering strategy is `evidence_tool_loop_v1`. Its checkpoint migration preserves
completed work, source revisions, measured counters and terminal stop-loss state. Legacy file plans
are historical context, not executable pending actions. A runtime upgrade alone does not reset a
failed run's repair budget. Explicit manual repair creates a fresh bounded attempt.

Research informs this design; it does not establish ForgeAI-specific performance:
[SWE-agent](https://arxiv.org/abs/2405.15793) studies agent-computer interfaces;
[mini-SWE-agent](https://mini-swe-agent.com/latest/advanced/control_flow/) provides a minimal
model/action/observation implementation; the 2026 survey
[Code as Agent Harness](https://arxiv.org/abs/2605.18747) discusses planning, state, feedback and
verification mechanisms. The four roles, staged commits and stop-loss settings are project-specific
engineering choices, not a paper reproduction. Deterministic regression tests do not measure model
quality; compare real-model runs on the same tasks and budgets before claiming an improvement.

### Engineering dependency scheduling

Product feature order is presentation order, never the construction schedule. Architect may freeze
`implementation_dependencies` between approved feature ids (for example authenticated identity before
private business operations). Code Engineer submits an `update_delivery_plan` graph using observed
source and those contracts before source mutation. Requirements and tasks have a many-to-many mapping:
shared foundations and multiple implementation slices must not introduce new product scope.

The runtime validates exact approved feature coverage, unique task ids, existing dependency targets,
acyclicity and frozen architecture dependencies. A visual foundation establishes or verifies reuse of
shared design tokens before feature work; it does not force a redesign of existing applications.
Authentication is added only when approved, not detected with title keywords or inserted as a universal
template. The scheduler selects only pending nodes whose prerequisites completed platform checks.
Ready-node ordering is an engineering plan choice, not the PRD's list position.

Replanning may adjust pending nodes but cannot remove or redefine completed nodes, invent completion
state, reset budgets or replace approved inputs. Graph revisions and reasons are checkpointed. Legacy
unfinished feature queues require a dependency plan before further writes while preserving completed
work and counters; completed runs remain immutable. QA repair retains its narrow defect scope and does
not recreate the whole application plan. Structural graph validation cannot prove the model identified
every semantic dependency; source checks and independent acceptance remain necessary.

### Workflow ownership

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
- Product Manager recovery and Architect, Code Engineer, Test Engineer and Deployment handoffs use
  idempotent database jobs.
  Development may embed one worker in the API process. Production disables the embedded worker and
  runs one or more `python -m app.worker` processes against the same database.
- Workers heartbeat both their job lease and attached task execution. Periodic recovery first requeues
  expired jobs, then replays old unapplied decisions and scans for legacy/orphaned expired executions,
  old pending tasks, and queued deployments that have no active job. A grace period prevents recovery
  from racing the request that originally created a decision or task. Conditional claims and active
  execution slots prevent duplicate workers from publishing the same result.
- Recovery scans identify candidates without locking joined project rows; claims lock only the
  eligible execution or task and skip busy rows. Healthy checkpoints must not contend with an
  unrelated recovery scan for their project foreign key.
- PM and Architect tool checkpoints retain frozen input, completed exchanges, draft state and spent
  model turns. Invalid tool responses receive bounded correction feedback; provider retries retain
  progress and do not reset budgets. Read-only tool batches execute within role and call limits;
  state changes remain sequential. New context and valid plan changes count as engineering progress,
  while repeated identical observations remain subject to the no-progress limit. Initial engineering
  planning has a separate bounded inspection allowance; after it is spent, the engineer must submit
  the dependency graph or report a concrete blocker. Source inspection remains available during
  implementation. The current task, observed-file summary, diagnostics and prior recovery reason
  follow historical tool exchanges in model context. Recovery never clears cumulative usage or
  reopens planning inspection; only an explicit manual retry extends the total call allowance.
  Budget exhaustion uses typed stop reasons, with old text-only records translated at the checkpoint
  compatibility boundary.
  Tools expose structured current diagnostics with locations, evidence, disposition and verification
  targets. Plan validation reports all detectable conflicts without weakening frozen dependencies;
  co-located prerequisite/dependent requirements still require explicit task separation under the
  current graph contract. The checkpoint retains distinct raw failures and source-bound correction
  attempts, while model context carries the latest plan/check exchanges and unresolved diagnostics.
  Work-item memory retains actual changed paths, bounded source excerpts and recent actions beyond
  the rolling tool history. Excerpts are hash-checked against the workspace before reuse; omitted,
  truncated and stale evidence is explicit. Writes and model-authored next-action notes do not prove
  completion. Repeated or excessive inspection triggers one bounded next-action correction per work
  item before stopping. Correction permits declared targeted reads, source edits or an explicit block;
  it never forces blind writes, changes approved scope or bypasses verification. Automatic recovery
  preserves correction usage; an explicit no-progress retry starts with corrective planning, not just
  extra call budget. The correction and all actions remain traceable in the fenced checkpoint/events.
  Source writes invalidate check evidence; unrelated checks cannot close other failures, and only
  independent QA resolves acceptance defects. Identical failed inputs have a bounded stop-loss that
  survives automatic recovery. Diagnostic adapters never invent missing causes or grant write access.
- An open BuildRun can await explicit recovery. User-facing status exposes the current job and retry
  schedule so queued recovery keeps polling while a stopped execution shows its cause and controls.
  Received model usage is metered before tool parsing, including malformed responses and Leader routing.

## Evaluation and release evidence

- Project Eval is production telemetry over persisted runs. It aggregates end-to-end success,
  acceptance results, first-attempt completion, Code Engineer repair rounds, routing-contract
  validity, unnecessary Leader use, trajectory completeness, security results, elapsed time, tokens
  and configured cost. Infrastructure retries are not counted as code repair rounds.
- Classification and routing accuracy are reported only when a human or benchmark label exists. An
  unlabelled production trace is marked unavailable, not counted as correct by construction.
- Fixed benchmark suites live under `benchmarks/`, are immutable by version and content-hashed when
  loaded. Scoring consumes captured observations and records per-case routing, architecture,
  end-to-end, security, repair-budget, trajectory, latency, token and cost evidence. Golden fixtures
  validate the scorer contract; they are not evidence of model quality.
- Schema first-pass and regression pass remain unavailable until attempt-level schema evidence and an
  independently labelled regression field are persisted; final success is never reused to invent them.
- CI builds the isolated check image and runs the pinned template through database migration, real API,
  frontend build and Chromium screenshot checks. Unit mocks remain useful but do not replace this gate.
- The initial publication provider is bounded Docker Compose: generated backend/frontend images and a
  PostgreSQL service, secret material outside the generated workspace, durable status/logs, and rollback
  to the previous deployment. Additional cloud providers must implement the same persisted contract.

## Prompt layering

Role prompts are assembled in four layers: hard platform boundaries, current workflow state, role
decision policy, and optional examples. Hard gates belong in runtime validation whenever possible;
prompts explain decisions without becoming the only enforcement mechanism.
