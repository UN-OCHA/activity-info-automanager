# Staged application/execution refactor plan

Prepared: 2026-11-19. Implements the contracts in
[execution-refactor-spec.md](execution-refactor-spec.md). The user subsequently
authorized stages 0-2, now completed; see the [stage 0 record](execution-refactor-stage0.md),
[stage 1 record](execution-refactor-stage1.md) and [stage 2 record](execution-refactor-stage2.md).
Stages 3 onward, dependency
installation and live API writes remain outside the current authorization.

## Scope and agreed tradeoffs

The initial migration covers existing commands through ModifyForm and MoveForm.
Keep NiceGUI, the safe parser, catalogs, creator definitions, transformations and
identity/formula machinery. AddForm/DeleteForm and new recovery endpoints remain
later features. No replacement framework or client upgrade is planned.

Multi-form Apply remains command-first; compatible schema Execute remains
form-first. Accept the isolated cross-form inconsistencies this can cause. Do not
build a general read/write dependency graph, consistency analyzer or dependency-
induced splitting. Keep explicit creator prerequisites, endpoint/group boundaries,
action-start identity membership, stale checks and recovery ownership checks.

Intentional changes needing explicit behavioral tests:

- Resource moves precede subsequent schema edits, replacing the adapter's reverse
  ordering. Compatible resource patches may share a request and rollback unit.
- Failed resolution publishes neither candidate state nor plan/variable changes.
  Rejected candidate data and paths are diagnostic attachments only.
- Each action-start field deletion has its own action. Later edits start a new
  schema action; schema-only recovery does not establish record-data restoration.
- RollbackLast skips attempts proven unchanged. Divergent outcomes block automatic
  recovery unless a handler establishes a safe partial-outcome contract.
- Historical displays retain their related schema/name/annotation context and
  pinned record labels; later cache changes do not alter them.
- Rename verification covers schema and resource labels, subject to endpoint
  evidence. Unresolved rename execution is blocked rather than guessed.
- Materialization precedes confirmation; execution consumes exactly that plan.

## Delivery and validation rules

Stages proceed in order after implementation is explicitly requested. Each stage
produces a reviewable checkpoint and records changed behavior, validation and known
limits. These are completion criteria, not additional per-stage permission prompts
when the user's implementation instruction already covers the work.

Use the project virtual environment. Run focused behavioral tests while developing
and the full suite at stage completion:

```powershell
.venv/Scripts/python.exe -m unittest discover -s test -t . -q
```

Stage 0 separately verified 393 passing current/reference tests and 356 passing
migration-base tests, using the same existing environment with the correct local
import roots. Keep recording both states accurately; their test counts differ by
the 37 preserved ModifyForm/MoveForm tests awaiting porting.
Preserve meaningful tests; mark intentional changes explicitly instead of changing
expectations merely to accommodate the implementation. Use endpoint mocks for
payload/order/failure tests and a mocked service for browser workflows. No live
writes, recovery experiments or new packages are required by the initial plan.

Keep old execution available until the replacement has its complete preflight,
verification and recovery path. Never send writes to both paths for comparison.
An internal compatibility adapter is temporary; it must not encode new resource
operations as synthetic schema properties. A stage may land an unwired module,
but it must not expose a partially safe Execute path.

## Stage 0 - Preserve work and establish the baseline

Completed 2026-11-19. Reference: `reference/pre-execution-refactor-20261119` at
`d1e9e6863f9fbd0ff10d07eb802812e95a8444f8`. Baseline branch:
`refactor/execution-state` in `../activity-info-automanager-refactor`, based on
`df131da`. See the [preservation record and test inventory](execution-refactor-stage0.md).

Deliverables:

- Inventory tracked and untracked work, including ModifyForm/MoveForm modules,
  tests and docs. Preserve it in an explicit checkpoint/reference branch or reviewed
  copy, keeping the ignored HANDOVER.md separately available. Do not indiscriminately
  add cache directories or credentials. Do not discard any untracked files.
- Verify the preserved copy and both new design documents before branch operations
  that could replace working files. Establish the refactor branch from `df131da`;
  carry the current spec/plan onto it. Do not reset or clean the original work.
- Run and record the preserved-current and migration-base suites separately.
- Inventory tests for porting, distinguishing legacy behavior from the intentional
  changes above. Retain ModifyForm/MoveForm tests for stage 6, rather than treating
  their temporary absence on the migration base as a completed regression check.

Completion: the current implementation is recoverable, the base is reproducible,
and the behavior-change/test inventory is recorded. No architecture code yet.

## Stage 1 - Canonical state and immutable results

Completed 2026-11-19. Canonical resource state and immutable result/display contracts
are integrated with the existing workspace and UI. Full suite: 373 passing tests
(356 baseline + 17 new behavioral tests). See the [implementation record](execution-refactor-stage1.md).
The Applier extraction and atomic error publication remain stage 2.

Deliverables:

- Resource keys, load/lifecycle states, baseline evidence, local revisions and
  ordered affected-resource keys. Separate canonical state from SDK models.
- Authored/application identities, results, DisplayState and diagnostic attachments.
  Retain read-only related rendering context without adding unwanted tabs.
- Defensive copy-on-write contracts and an adapter for existing cards/results.
  Preserve authored text, parent/child counts, selection and annotations.
- Pin historical record labels and define generation-checked immutable enrichment
  for the current display only. Retain revision references across history.

Checks: later state/action edits cannot mutate earlier results; related-form edits
cannot change historical rendering; zero-match parents and unexecuted children
retain accurate metadata; display selection does not change application context.

Completion: state/result contracts can serve the existing UI without relying on
mutable action payloads or reconstructing canonical state from displayed JSON.

## Stage 2 - Extract the Applier and atomic staging

Completed 2026-11-19. Applier now owns local/connected evaluation, expansion and
atomic publication of state, context, variables, identities, snapshots and effects.
Rejected candidates are separate immutable attachments with a labelled UI inspector.
Full suite: 386 passing tests (373 stage-1 + 13 new tests). See the
[implementation record](execution-refactor-stage2.md). API plan compilation remains stage 3.

Deliverables:

- Move evaluation loops out of CommandListManager; keep list editing and selection
  there. Reuse command transformations and PreparedCommand/template machinery.
- Stage state, variables, context, identities and effects per actual application;
  commit together only after transformation and required resolution succeed.
- Preserve frozen form/field targets, command-first iteration, scoped variables,
  complete planned counts, warning behavior and async cancellation/generation guards.
- Retain rejected resolution candidates separately for error inspection. Candidate
  diagnostic paths never annotate the preceding successful resource snapshot.

Checks: atomic resolver/transform failures; no API writes during Apply/suggestions;
single-pass substitution including escaping; local-over-remote resolution; inherited
variable cleanup; early-failure counts; stale async reads cannot publish state.

Completion: application is callable without UI orchestration, and current parser,
field-edit, variable and iteration behavior passes except documented atomicity changes.

## Stage 3 - PlanBuilder, grouping and deletion boundaries

Deliverables:

- Typed effects, ordered groups, candidate maps and action/application provenance.
  Keep A/B accumulation and simple local compatibility rules from spec section 9.
- Seal groups on context-scope exits and explicit schema/resource/deletion boundaries.
  Expose action sequence independently of command/application order.
- Track creator action and action-start field membership, including preview IDs.
  Isolate one persisted field deletion per action; subsequent edits use its result.
- Eliminate same-action add/delete where allowed; retain existing DeleteField
  validation semantics without a new general referential-integrity feature.

Checks: A1,B1,A2,B2 applies into accumulated A/B writes; same-form context no-op;
ordered revisits; no writes for context/variables; exact deletion/action sequences;
temporary-field counterexample; snapshots survive later candidate extensions.

Completion: Apply produces an inspectable candidate plan with stable provenance.
No cross-form dependency analyzer is required. Resource-boundary tests may use typed
fixtures until real resource commands arrive in stage 6.

## Stage 4 - Handlers, adapter evidence and frozen confirmation

Deliverables:

- Handler protocol and schema/deletion handlers using generated endpoints through
  ActivityInfoService. Keep SDK encoding at the adapter boundary.
- Structured observations/errors distinguishing missing, unavailable, forbidden and
  invalid reads. Preserve available response receipts and versions.
- Reuse schema equivalence for schema handlers; define separate property/existence
  comparison for resource handlers. Do not reuse missing/null/false schema defaults
  indiscriminately for resource patches.
- Materialize all pending identities once per execution attempt, preserving formula
  literals, historical previews and cross-action/record-label references.
- Confirmation consumes the immutable materialized action list, effects, targets,
  destinations, deletions, contributors and recovery limits. Bind it to evaluation,
  source and session identity; changes/cancel invalidate the attempt.

Checks: exact generated payloads, structured failures, receipts retained, stable IDs,
no execution-time substitution/re-query, immutable confirmation, invalidation, and
no writes before confirmation. No newly exposed Execute path until stage 5 passes.

Completion: handlers can encode/read/compare and the reviewed plan is the exact plan
the new Executor will receive. Endpoint write success remains distinct from proof.

## Stage 5 - Generic execution and verified recovery

Deliverables:

- All-required-baselines preflight, immediate predecessor checks and no-op skips.
- Journal attempts before each write; record defensive immediate preimages,
  read-back state on success/error, receipts and separate recovery history.
- Classify intended, unchanged, divergent and unknown outcomes. A reread matching a
  divergent postimage does not establish ownership; initially block its compensation.
- Leave/RollbackLast/RollbackAll, reverse verified compensation, reconciliation of
  unknown results, stop on unsafe/failed recovery, and truthful partial reports.
- Preserve schema-only persisted-field recovery with explicit data-unverified limits.
  Clear stale caches without altering the retained plan, journal or report.

Checks: stale preflight before any write; revisits; timeouts before/after commit;
reported error despite committed state; wrong-state success; unchanged failure
selecting the prior changed action; unknown reconciliation; divergent state blocked;
recovery errors read back and stop; concurrent edits preserved; stable final reports.

Completion: cut over existing schema execution only after the complete safety path
passes mocked end-to-end tests. Keep the old path isolated for unported reference
work until stage 6; never mix old and new journals within one execution attempt.

## Stage 6 - Port form commands and resource display/execution

Deliverables:

- Port preserved ModifyForm/MoveForm syntax, creators, selection/scope behavior and
  suggestion-generation guards. Retain optional-field semantics and folder rules.
- Canonical database resources, batched property patches, destination existence
  checks, owned-property compensation and resource-version evidence.
- Coherent local rename projections. Inspect installed-client/official endpoint
  evidence for schema/resource label side effects. Record the evidence and the
  handler's verification footprint. If a second write is necessary, specify/test
  its order and compensation before enabling it; otherwise keep rename execution
  blocked with an explicit reason. No live experiment is authorized by this plan.
- Resource tabs with independent historical snapshots/annotations and changed-tab
  preference. Moving forms changes context without mutating schema payloads.
- Execute moves before subsequent schema edits and show the actual action order.
  Remove parallel folder arrays and retire form_move_execution.py when its final
  caller is migrated.

Checks: all preserved form-command tests ported, with intentional order/rollback
expectations rewritten explicitly; resource patch batching; unrelated properties
preserved; destination removal; rename side effects and unknown-contract gate;
mixed reverse rollback; no synthetic schema metadata; full snapshot/tab selection.

Completion: both resource types use one plan/journal model. Any unresolved rename
contract is a documented capability limit, not a claimed successful migration of
rename execution; that slice remains incomplete until evidence resolves it.

## Stage 7 - Integration, retirement and handover

Deliverables:

- Run the complete suite and mocked browser workflows: author/apply, expanded child
  selection, schema/resource tabs, confirmation/cancel, execution, all recovery
  choices, stale edits, reports and reevaluation.
- Remove obsolete application loops, schema-visit planning, temporary compatibility
  adapters and execution paths after their replacement coverage passes. Retain the
  useful comparison, identity, resolution and transformation helpers.
- Update architecture/behavior docs and HANDOVER.md with actual modules, test
  results, behavior changes, accepted batching limits and unresolved API evidence.

Completion: no active writes depend on schema-shaped resource metadata; UI consumes
results, Executor consumes confirmed plans, reports consume observations. Every
supported existing command has migrated or an explicit incomplete capability is
reported. Do not call the full migration complete with rename execution unresolved.

## Separate later feature stages

1. AddForm: creator syntax, creation handler, stable new-form identities, explicit
   creator-before-use prerequisites, schema/resource verification and conditional
   compensation. Optional create/delete elimination may be omitted; no general
   cross-form analyzer is implied.
2. Recovery investigation: in a separately authorized test environment, establish
   field recovery and database-version revert semantics, record-data restoration,
   supported client endpoints and concurrency limits.
3. DeleteForm/richer field recovery: make the product/recovery decision using that
   evidence, then implement lifecycle/UI behavior, truthful capabilities and tested
   compensation. Empty schema recreation is not accepted form recovery.

These are not part of the initial migration's implementation or test completion gates.
