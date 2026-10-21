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
  Source creation and whole-file replacement use one bounded write interface for one or more files.
  Related writes share preflight validation and fenced commit; local replacements remain a separate
  operation. File count does not define a distinct workflow or justify implementing future work.
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

For an empty initial plan, the platform offers a complete, validated proposal derived from frozen
feature ids, linked acceptance and explicit architecture dependencies. It contains a reusable visual
foundation and one feature node per approved capability; it does not infer authentication from words
or invent product scope. Cross-feature acceptance is assigned only when its linked prerequisites are
available. The engineer explicitly adopts the displayed proposal by its content hash or submits a
custom graph. Adoption uses the same validation, fencing, history and budgets; it is not automatic
completion or product approval. An existing plan and independent QA repair cannot be replaced through
this shortcut. Changed proposals require a fresh matching hash.

The runtime validates exact approved feature coverage, unique task ids, existing dependency targets,
acyclicity and frozen architecture dependencies. A visual foundation establishes or verifies reuse of
shared design tokens before feature work; it does not force a redesign of existing applications.
Authentication is added only when approved, not detected with title keywords or inserted as a universal
template. Requirement links express traceability, not proof that a shared visual task implements every
linked business capability. A work item may explicitly declare `provides_requirement_ids`, a subset of
its linked approved requirements, with acceptance and deliverables. These declarations can satisfy
architecture prerequisite edges only through ancestors. Visual foundations without declarations do not
provide business prerequisites; nonvisual legacy items retain their existing linked-requirement
interpretation when the optional field is empty. A nonempty declaration is the item's explicit provider
set. It never grants feature coverage or completion: all approved requirements still need feature work,
and declared providers must pass the same source-bound `all` integration gate before dependents run.
Structural validation cannot establish the truth of a declared capability; smoke and independent QA
remain necessary. The scheduler selects only pending nodes whose prerequisites completed platform checks.
Ready-node ordering is an engineering plan choice, not the PRD's list position.

Replanning may adjust pending nodes but cannot remove or redefine completed nodes, invent completion
state, reset budgets or replace approved inputs. Graph revisions and reasons are checkpointed. Legacy
unfinished feature queues require a dependency plan before further writes while preserving completed
work and counters; completed runs remain immutable. QA repair retains its narrow defect scope and does
not recreate the whole application plan. Structural graph validation cannot prove the model identified
every semantic dependency; source checks and independent acceptance remain necessary.

Feature work items represent runnable user capabilities and may span frontend, backend and database.
Foundations validate only their own available contracts; they must not require a downstream feature
that depends on them. Completion selects a single check from platform-observed source changes.
Known source foundations may use `quick`; changes limited to source styles or public raster images
relative to the last passing `all` snapshot may use `frontend`. Business, persistence, permission,
configuration, unknown changes and unresolved runtime failures use `all`. Explicit prerequisite
providers, independent QA repairs and the final work item always require `all`. Missing baselines
fall back to full verification. Local success retains pending integration and cannot publish a final
code result. Engineering extends the mutable smoke manifest with the
current capability and retains probes for already implemented behavior; unimplemented scenarios enter
with their owning work item. Final completion still requires `all` and independent QA. Passing smoke
proves only the probes executed, not semantic coverage of every approved requirement.

The `all` runner performs syntax and TypeScript checks once, followed by frontend build, migrations,
API smoke and browser smoke in the same container. A failed early stage prevents costly later stages.
Check cache reuse requires both exact source hash and runtime version. Host elapsed time and bounded
runner phase observations distinguish execution from cache reuse; timings are diagnostic data, never
quality authority. A targeted repair recheck cannot resolve a broader failed gate. A passing `all`
may resolve the narrower checks it actually includes, with explicit coverage and source provenance.

Engineering reuses the template's API envelope/error handling and database/migration primitives.
Authentication remains opt-in to approved intent. After backend contracts are implemented,
`generate_api_client` exports the actual FastAPI OpenAPI in an isolated container and invokes the
image-pinned generator with platform-owned configuration. It owns `frontend/openapi.json` and
`frontend/src/client/`; business wrappers remain outside that directory. Generated changes and obsolete
file removal use the same execution fence and recoverable revisions, additionally comparing the full
input source hash before commit. Once adopted, a changed backend or generator runtime invalidates the
client contract and requires regeneration before checks. Client generation itself proves no acceptance.

Smoke manifest validation shares one contract across staged writes, source-snapshot preflight and
the isolated runner. Quantity errors identify the case and actual limits before dependency setup or
database migration. Long journeys and additional cases share a bounded aggregate action budget and
wall-clock timeout. Explicit UI-only cases require observable assertions; integration cases retain
API evidence. Expected HTTP failures match an exact method, local path and status. Only the matching
observed browser resource error is exempted; unexpected responses, application console errors and
script errors still fail. Frontend compilation precedes runtime database setup.

Engineering check outcomes and freshness are separate facts. Source changes invalidate cached checks
and mark recorded evidence as requiring revalidation; they never reopen a previously resolved defect.
Only an actual failed check can reopen that defect. Previous failures may guide investigation but are
explicitly historical after source changes. Model context excludes superseded failure exchanges while
durable audit history keeps them. Independent QA defects can only be resolved by independent QA.

Engineering tracks returned file-version/range coverage separately from source and verification
progress. Changing read arguments, rereading overlapping ranges, or replaying a cached check does not
create new progress. Continued inspection receives one replaceable current-state guidance note, with
all implementation tools still available. After a grace period, continued lack of source/check
progress pauses the attempt under the existing recoverable stop policy. These thresholds are bounded
engineering heuristics to evaluate, not a semantic proof that investigation is unnecessary. Automatic
recovery preserves counters and guidance; only explicit retry grants another bounded attempt.
Outstanding failed checks also retain a separate bounded counter across edits and observations.
Only a passing result for that check clears it; unrelated passing checks and arbitrary source edits
cannot indefinitely defer a recoverable stop. This remains a heuristic, not proof of product progress.

Architect may save validated top-level design sections incrementally and append bounded list entries.
The checkpoint preserves these incomplete drafts across interruptions. Final submission validates the
complete assembled design and frozen dependencies; saving sections alone never completes architecture.
Code Engineer may opt into provider thinking through `CODE_ENGINEER_THINKING`; its mode is
retained in the execution checkpoint. Provider reasoning is opaque protocol state, stored with its
original assistant tool batch before execution and replayed only with matching results. Model-turn
identity prevents reused call ids from mixing batches. History compaction omits a whole native batch
when its results are incomplete; it never truncates reasoning or duplicates it into tool results or
user instructions. Logs record mode and length, not reasoning text. Existing nonthinking checkpoints
remain readable, and enabling thinking does not change source isolation, gates or retry budgets.
Responses truncated by the provider execute no tools and retain metered usage. Recovery reduces the
next operation size while retaining previously saved sections, rather than resubmitting the full draft.

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
  while repeated identical observations remain visible as evidence. Initial engineering
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
  Plan correction also carries a bounded structural summary of recent failed submissions since the
  last accepted plan. Repeated conflicts are matched by rule and affected requirement or dependency,
  independently of cosmetic plan changes; returning conflicts flag possible regressions, not proof
  that an earlier plan had satisfied a check omitted by a prior validation failure. This evidence
  prompts comparison of the combined constraints before another submission. The latest full graph
  remains available, historical summaries are explicitly partial, and a valid plan clears the current
  repair context without deleting audit evidence. This guidance grants neither extra attempts nor
  permission to relax coverage, frozen dependencies or completed-node protection.
  Work-item memory retains actual changed paths, bounded source excerpts and recent actions beyond
  the rolling tool history. Source evidence is keyed by path, source hash and read range: complementary
  ranges coexist, identical range reads are deduplicated, and a new source version replaces all old
  fragments. The latest native exchange for each range remains visible. Recovery can seed compatible
  ranges from saved observations without resetting usage or resuming a stopped run. Per-file cached
  text and aggregate projected source text are bounded; omitted ranges are explicit. Source versions
  remain known even when their text is omitted from the context budget.
  Excerpts are hash-checked against the workspace before reuse; omitted,
  truncated and stale evidence is explicit. Writes do not prove completion. Repeated inspection
  during implementation does not disable tools or require a model-authored correction plan; total
  call budgets still bound execution. Historical focus state is retained only for audit on upgrade.
  Protocol refusals apply to the tool set offered for that response: a changed set supersedes them,
  and a validated model tool call resolves them without clearing unrelated failures. Protocol errors
  are current diagnostics, not fictitious tool-call history. Recovery reasons are explicitly historical;
  each decision receives the actual current tool list. Migration never resumes a stopped execution.
  Source writes invalidate check evidence; unrelated checks cannot close other failures, and only
  independent QA resolves acceptance defects. Identical failed inputs have a bounded stop-loss that
  survives automatic recovery. Diagnostic adapters never invent missing causes or grant write access.
- An open BuildRun can await explicit recovery. User-facing status exposes the current job and retry
  schedule so queued recovery keeps polling while a stopped execution shows its cause and controls.
  Received model usage is metered before tool parsing, including malformed responses and Leader routing.

## Startup context, optional capabilities and progress presentation

Engineering receives a bounded startup pack read from the current isolated workspace's known entry
points, configuration and contracts. Every excerpt carries its actual file hash and line range;
oversize, absent and changed files remain explicit. The pack is reused only while the bytes match,
and a later partial read cannot erase complementary startup evidence. It consumes no model/tool
turns, proves no implementation progress and grants no new write permission. Existing source edits
still require the supplied observed hash and the fenced original-byte check. Dependency and runtime
directories are pruned before indexing; startup does not traverse installed packages.

Platform capability availability is distinct from product scope and installation. Architect may
select a versioned capability only for explicitly linked approved features; the semantic fit remains
an architecture responsibility. Empty or legacy selections enable nothing. Engineering freezes the
selected bundle's source digest with the design. Installation is offered only for selected bundles
and stages source through the existing execution fence, optimistic checks and recoverable revisions.
A changed bundle, existing target, customized entry point or migration chain cannot be overwritten.
The first bundle, `auth-session-v1`, supplies local password verification, expiring/revocable bearer
sessions, current identity, migration and in-memory React auth state. It supplies no default account,
public registration or business ownership policy. Approved provisioning, UI and record-level access
must still be implemented and checked; capability installation never completes a work item.

The conversation groups routine reads/writes into compact descriptions with expandable evidence.
Failure, actual repair and verification remain distinct so later success cannot hide the reason for
a retry. Internal decision calls do not become extra user-visible steps. The source browser exposes
read-only REQUIREMENTS, ARCHITECTURE and PROGRESS documents from the current run's persisted artifacts
and exact execution lineage, including before generated source is available. PROGRESS reflects only
accepted work and recorded checks, distinguishing engineering completion from independent acceptance.
Key execution changes refresh these documents without replacing the user's selected file. Document
projections do not modify generated source, source hashes or approved intent.

Plan correction summaries live in their own bounded, ordered checkpoint history, independent of
rolling tool observations. Extra reads cannot erase unresolved plan attempts. A successful plan
starts a new summary epoch while audit history remains durable. Legacy missing history is labelled
partial rather than reconstructed from deduplicated diagnostics. This mechanism changes no budgets,
completion state or stop-loss boundaries.

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
