# ForgeAI Artifact and Workflow Reference

## Artifact ownership

- `app_spec`: Product Manager; becomes delivery input only after explicit user approval.
- `system_design`: Architect; derives from one exact approved `app_spec`.
- `acceptance_test_plan`: platform-owned frozen approved standards/identities for exact engineering input.
- QA suite: Test Engineer scenario/script draft, validated and versioned by the platform as JSON;
  `suite_hash` identifies executable assets independently of the approved-standard `test_hash`.
- `code`: Code Engineer; content-addressed source identity and provenance.
- `test_report`: Test Engineer; immutable evidence for one exact `source_hash`, `test_hash` and
  `suite_hash`, with runner-owned per-case observations and execution ids.

## Scheduling

- Leader decides ambiguous semantics and emits structured `NextAction`.
- A validated `NextAction` is persisted before side effects under an idempotent decision key; replay
  reuses that exact payload and records applied/failed outcome.
- The workflow engine validates gates and applies legal transitions.
- Deterministic single-path handoffs do not re-enter Leader: Architect→Code Engineer,
  published code→Test Engineer (mandatory acceptance gate).
- Quality failure `NextAction` kinds are derived from typed report state:
  `retry_infrastructure`, `dispatch_code_engineer`, or `await_user_challenge`. QA does not dispatch
  Architect from free-form evidence; a product/design change starts a newly approved lineage.

## Compatibility

- A repair may create a new `code` and `source_hash` while retaining the same approved `app_spec` and `test_hash`.
- A repair inherits the QA suite from its predecessor report and must rerun it on the new source.
  Endpoint/selector adaptation may produce a new suite version, but cannot delete scenarios,
  operations, input data or assertions. A visibility-only wait can be inserted or removed without
  changing that contract. Expected-result changes require a challenge.
- Acceptance plan schema v3 names independent QA execution. Report schema v3 embeds the suite;
  QA checkpoint v5 retains version history, execution records, spent turns, defect-resolution audit
  and up to three explicit budget grant attempt ids. v1/v2/v3/v4 checkpoints
  and QA runner v1 executions remain readable; only current runner-bound evidence can satisfy a new
  passing publication.
- A product change creates a new approved `app_spec`, acceptance plan and `test_hash`; it does not mutate the old frozen plan.
- A change after a terminal run creates a new BuildRun and explicit `RunRevision`; old artifacts and
  execution state remain immutable. Implementation repair may clone the source workspace but must
  publish new code and acceptance evidence.
- `forgeai.smoke.json` is mutable generated source owned by engineering. Acceptance assets are frozen and cannot appear in an engineering file plan.
- A `test_challenge` means the frozen test and approved intent appear inconsistent. It requires an explicit resolution before engineering continues.

## Interaction targets

- Data read/write: real API `method/path`.
- Navigation: real frontend route.
- Pure UI behavior: component event or frontend state.

## Visual profiles

- `expressive`: content, showcase and consumer products; includes imagery strategy.
- `standard`: ordinary business applications; imagery only when useful.
- `utilitarian`: internal tools, admin systems and dashboards; no mandatory illustration.

## Runtime and release

- Pause blocks new job claims for the BuildRun, releases active leases, fences any active execution,
  and preserves queued work and checkpoints for explicit resume.
- Eval metrics come from persisted runs, reports, decisions and model-usage events. Accuracy requires
  external labels; absent labels are reported as unavailable.
- Preview is local inspection. Deployment is a separate accepted-source record with provider status,
  secret reference, image identity, URL, logs and rollback lineage.
