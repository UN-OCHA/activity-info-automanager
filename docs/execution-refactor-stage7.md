# Stage 7: integration, retirement and handover

Subsequent update: the user clarified the shared-label contract and authorized
[rename execution](execution-refactor-rename.md), which is now implemented. The
remaining capability described below records the stage-7 checkpoint's state.

Completed the supported integration/retirement work on 2026-11-19 after explicit
stage-7 authorization. **Rename execution remains an incomplete capability**, as
documented in the [stage-6 evidence record](execution-refactor-stage6.md). Do not
describe the entire migration as complete while this slice is gated.

## Retired execution and visit planning

Removed apply_form_schema_change and apply_schema_sequence, including their
separate write, verification and rollback loops. schema_change.py now contains
only reusable schema comparison/model-validation helpers. Removed prepare_operations;
confirmed_plan.py is the sole plan materializer used for execution.

FormWorkspace no longer stores mutable schema visits, original-schema mirrors or
active-slot indexes. It owns canonical ApplicationState, selects forms by ResourceKey
and exposes detached current/baseline projections. Frozen form scopes retain keys,
not array positions. Application results and card summaries call their detached
schema display projections form_snapshots; these are not execution schedules.
CommandListManager.target_schema/target_forms read canonical state/immutable result
views rather than mutable card JSON. Revisits retain action order in CandidatePlan
while display tabs have one current resource per key; earlier DisplayStates remain
unchanged. No general cross-form dependency analysis was introduced.

The page now renders selected authored/actual application results from
CommandListManager.results and DisplayState. Removed the fallback that scanned
earlier mutable card snapshots to reconstruct annotations/context. Reports retain
their separate immutable display context. Source loading still renders its own
read snapshot. Selecting a history tab does not alter the application context.

There is one runtime Executor/journal/recovery path for schemas and supported
resource patches. Generated write adapters and useful identity, comparison,
resolution and transformation helpers remain. No form_move_execution adapter,
parallel location arrays or synthetic schema metadata were introduced.

## Regression migration and integration fixes

Preserved all 499 stage-6 tests and migrated the tests of retired execution functions
to typed candidates, materialized confirmation and the production Executor.
test/execution_fixtures.py supplies test-only plan construction and a dict-service
mock adapter; it has no independent execution, retry, journal or recovery loop and
is not imported by application code. Existing generated-payload assertions and
reference/default/formula tests continue to check actual payloads.

Assertions explicitly account for preflight plus immediate reads, no-op plans with
no invented actual observation, identity allocation before stale preflight, isolated
actions rather than mutable visit arrays, and divergent outcomes blocking automatic
recovery. Revisit recovery scenarios use schema description edits so they exercise
the supported handler instead of bypassing the rename gate. Error-after-commit
fixtures establish an intended postimage before requesting compensation; tests of
genuinely divergent results retain the blocking assertions. No meaningful checks
were skipped or removed merely to retire the old code.

Migration exposed an integration bug: reference-default cache keys embedded Python
type objects, which the frozen confirmation model correctly rejects. schema_names.py
now uses string type tags and scalar text in these keys, retaining numeric/string
distinctions. A new real-page regression resolves a reference default during Apply,
confirms and executes without repeating name/record queries, and checks the retained
immutable alias and written record identity. Divergent verification reports now
retain comparison diagnostics in addition to outcome classification.

The second new page regression changes server state after Apply and confirms that
preflight reports the observed stale schema, performs no writes and survives cache
clearing/reevaluation. Existing mocked UI workflows cover authoring/Apply, expanded
child selection, form/resource tabs, confirmation/cancel/edit/source invalidation,
all recovery choices, reports and reevaluation. Endpoint fault tests cover committed
errors, unchanged failures, unknown reconciliation, unsafe divergence, concurrent
edits, recovery errors and interruption. These are mocked NiceGUI workflows and
endpoint tests, not a live browser/server experiment.

## Documentation and verification

README.md, command-iteration.md and the new
[current architecture reference](execution-architecture.md) describe the actual
modules, action ordering, supported commands, immutable display/report boundaries,
accepted command-first Apply/form-first schema batching limitation, conservative
recovery rules and remaining endpoint evidence gates. Historical stage records
remain unchanged as checkpoints.

Full suite: **501 tests pass in 34.037 seconds**, exit 0, no failures or skips.
This is all 499 stage-6 tests plus two new UI integration regressions. The original
393-test reference remains a separate stage-0 historical result. `git diff --check`
is clean. A source search finds no retired execution functions, prepare_operations,
active_slot, location_visits or synthetic location metadata in activity_manager.

Used the existing virtual environment with `-B`; no packages, live writes or
recovery experiments were introduced. Original reference source/tests remain
untouched. Both handovers point to this checkpoint.

## Remaining capability work

Rename execution still needs verified schema/resource label side effects and, if
two writes are required, their ordering and compensation contract. Existing docs
do not establish that behavior, and no live experiment is authorized. Preparation
continues to block the entire rename attempt before writes. Record-label-only
ModifyForm and parent-only MoveForm remain supported.

Persisted-field compensation remains schema-only/data-unverified. Optimistic
read-check-write retains a concurrency window. AddForm/DeleteForm, arbitrary
property clearing and new recovery endpoints remain separate feature/evidence work.
