# Stage 3 candidate action planning

Completed: 2026-11-19, on refactor/execution-state. Stage 4 has not started.
See the [specification](execution-refactor-spec.md) and [plan](execution-refactor-plan.md).

## PlanBuilder and inspection

planning.py owns typed SchemaEffect/ResourcePatchEffect and immutable CandidatePlan,
ApiAction, ActionGroup, Footprint, MemberRef, MemberProvenance and PropertyPatch.
SchemaEffect remains importable from applier.py for compatibility.

Applier and CommandListManager expose candidate_plan. It contains successful
application/effect order separately from API action order, groups, required baselines
and creator provenance. Actions retain IDs, logical subjects/write owners, expected
predecessors, projected results, contributors, explicit prerequisites, footprints,
action-start membership and recovery metadata. Actual result action_ids resolve
their effect IDs against current active actions.

To inspect an existing manager without writes:

    plan = manager.candidate_plan
    [(action.kind.value, action.key.identity, action.application_ids) for action in plan.actions]

These are unmaterialized compilation values, not confirmed requests. Inspection
does not allocate real IDs, reinterpret commands or contact endpoints.

## Groups and boundaries

Explicit epochs and a resource/capability candidate map preserve command-first
application and first-write/form-first A/B schema execution. Scope replacement/exit
seals old candidates, including switching to the already active form when ending
a scope. Same-form WithForm outside a scope remains a no-op boundary-wise. Ordinary
revisits retain order; there is no global regrouping by form or endpoint.

Schema/resource transitions seal candidates. Adjacent compatible database patches
may batch properties/entries; intervening schema effects prevent reuse. Context,
variables and suggestions create no API actions or empty action groups.

No general cross-form dependency analyzer or formula-reference scan was added.
The accepted batching limitation remains. Only explicitly declared creator/member
and action prerequisites constrain reuse; unknown explicit prerequisites fail
compilation atomically. DeleteField gained no referential-integrity feature.

## Creator provenance and deletion

Local field creation is tied to an action/application independently of pending-ID
spelling. Baseline members have no local creator. Actions separately retain
action-start members and local creation history.

A same-action creation/deletion may collapse to no request. Its trace remains in
omitted_actions; application counts and historical previews remain intact. Current
result action links exclude omitted requests. Creation history does not prove a
member survives in the final payload; materialization must inspect membership.

Deleting an action-start field produces one PersistedFieldDelete action, sealed
before and after. Subsequent edits start from its result. Pattern deletions already
flatten to one-field applications. The temporary-field counterexample compiles as:
schema-create-x, resource move, schema-add-y, persisted-delete-x, although x still
has a preview ID during Apply.

Persisted deletion candidates retain identity/preimage and explicitly report
schema-only compensation with record-data restoration unverified. No recovery
endpoints or stronger record-data guarantees were added.

## Resource fixtures and immutable publication

ResourcePatchEffect fixtures cover database operations until MoveForm is ported.
Only declared properties of existing entries may change; unrelated entries/properties
and ordering are preserved. Ownership footprints cover those properties rather than
the whole database. Logical form subjects differ from the database write owner.
Folder existence can be an explicit member prerequisite.

PropertyPatch retains presence separately from values, including absent/null and
nested scalar types. Clearing is represented without assuming omission clears a
server property; request encoding remains a handler-stage responsibility.

Builder.add compiles on a private copy and publishes only complete valid changes.
PlanBuilder is also part of the Applier's staged/detached ApplicationDraft publication.
Application, resolution, capture, compilation and context failures leave the published
plan unchanged. Retained drafts cannot extend it; later action extension cannot
mutate prior plan or display snapshots.

## Validation and runtime boundary

Full suite: **408 tests pass in 14.304 seconds**, exit 0, no failures or skips.
The 386 stage-2 tests remain intact, plus 22 tests in test_planning.py. Coverage
includes exact A/B/revisit order; scope replacement/exit/failure; same-form no-ops;
creator membership versus temporary prefixes; elimination and persisted/pattern
deletions; resource batching/ownership/presence/type handling; endpoint boundaries;
explicit prerequisites; snapshot isolation; retained drafts; and atomic failures.

Tests used the existing virtual environment and mocked endpoints. Original Python
sources remain unchanged. No packages or live API writes were added.

Execute still consumes legacy ordered schema visits. These candidate grouping and
deletion boundaries do not yet change runtime requests. Stage 4 adds handlers,
structured observations/receipts, materialization and frozen-plan confirmation.
Stage 5 cuts execution over only with complete preflight, verification, journaling
and recovery. ModifyForm/MoveForm/resource tabs remain stage 6; AddForm/DeleteForm
and richer recovery remain separate later features.
