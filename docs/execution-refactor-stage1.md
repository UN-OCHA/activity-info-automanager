# Stage 1 canonical state and immutable results

Completed: 2026-11-19, on refactor/execution-state in the isolated refactor worktree.
Stage 2 has not started. See the [spec](execution-refactor-spec.md) and
[staged plan](execution-refactor-plan.md).

## Implemented contracts

- immutable.py provides recursively frozen structured mappings/sequences and
  detached decoding for the legacy UI. SubstitutedText annotations survive capture
  and decoding without retaining mutable span objects. Unsupported objects,
  including mutable SDK models, are rejected. Mapping equality ignores property
  order but distinguishes JSON scalar types so changed payloads get new revisions.
- resource_state.py provides typed form/database keys, form and database-resource
  payloads, load/lifecycle states, successful presence/absence evidence, a baseline
  store, local revisions/provenance and separate affected-resource encounter order.
  Database entries retain ordering, scoped identities and unrelated properties.
- ApplicationState owns current resource values. A successful read can replace
  an unavailable observation, but a cached baseline never overwrites a local loaded
  revision. Failed reads do not establish absence. Published values are immutable.
- application_results.py provides authored and actual command identities, results,
  rejected-candidate diagnostic attachments, display resources and DisplayState.
  Concrete parameters/syntax are captured after preparation. Field member IDs remain
  available after deletion and for planned but unexecuted iterations.
- Display snapshots retain related schema/name context, pinned record labels,
  annotations and preferred tab. Read-only related resources need no tab. An
  unavailable resource remains explicitly unavailable rather than becoming empty
  schema data. Deleted lifecycle values retain their last schema for future UI work.

## Existing implementation integration

FormWorkspace now owns ApplicationState for latest form values. Its ordered visits,
original-schema mapping and active slots remain compatibility input for the current
executor. They are not used as the canonical latest-state store.

CommandListManager still owns the application loops. It assigns stable authored
identities across reordering, captures snapshots at result boundaries and exposes
immutable AuthoredCommandResult values through its results property. Zero matches
retain their parent result; known unexecuted children have target metadata but no
invented post-state. Existing cards keep their compatibility fields and counts.

The page renders command results from each DisplayState, including its captured
context and metadata. It no longer needs later caches or history scanning for these
snapshots. Historical command tabs do not query live record labels. Tab selection
does not change the active application resource. Source/report compatibility views
remain on the existing display path until their later migrations.

enrich_current_display checks the evaluation generation and current snapshot ID,
then publishes a new immutable presentation revision. Existing labels stay pinned;
earlier snapshots, canonical values and execution payloads stay unchanged. Async
source-label rendering also captures its context and checks generation/selection.
No new live query workflow was added for command history.

Offline schemas without server IDs remain supported. They get a local preview key
and NEW lifecycle in canonical state, without adding an ID to the authored schema
or recording remote-presence evidence. This preserves existing offline behavior.

## Validation

The complete suite passed: **373 tests in 17.148 seconds**, exit code 0,
with no failures or skips. This includes all 356 migration-base tests plus 17 new
behavioral tests in test_resource_state.py and test_application_results.py.

Coverage includes input/accessor/legacy mutation isolation; substitution annotations;
baseline/local revisions and scalar type changes; unavailable versus absent reads;
database entry ordering/scoping; read-only context without extra tabs; lifecycle
history; offline previews; concrete IDs/parameters; stable authored identities;
zero matches and unexecuted targets; guarded enrichment; pinned labels across
context-only commands; and UI rendering after later cache/payload mutation.

Existing parser, creator, iteration, history/tab, formula and mocked execution/
recovery tests remain intact. Tests used the original checkout's existing virtual
environment with imports from the refactor worktree. No dependencies were installed
and no live API writes or recovery experiments were performed.

## Deferred to the planned stages

- Stage 2 extracts the Applier and makes transformation/resolution failure publication
  atomic across canonical state, variables and effects. The rejected-candidate
  attachment contract exists, but the legacy failure path has not yet been migrated.
- Stage 3 replaces ordered-visit planning and adds action/creator provenance.
- Stages 4-5 migrate materialization, confirmation, handlers and the runtime journal.
- Stage 6 ports ModifyForm/MoveForm, coherent rename verification and database-resource
  tabs. Their 37 reference tests remain preserved, not yet part of the 373-test suite.
- No general cross-form dependency analyzer was introduced. The accepted command-first
  application/form-first schema execution tradeoff remains.
