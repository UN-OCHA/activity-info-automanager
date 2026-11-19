# Rename execution: one shared label, two representations

The user clarified that the form schema and database resource expose the same
label, writable from either interface, and explicitly authorized finishing rename
support. This supersedes the uncertainty recorded at stages 6 and 7. We adopt that
supplied contract; no live endpoint experiment or independent server-atomicity claim
is made. ModifyForm uses the schema endpoint exclusively.

## Planning and application

SchemaEffect and ApiAction can retain immutable ResourceProjection values. The
Applier updates the schema and database-label projection atomically. It no longer
emits a separate ResourcePatchEffect for the rename. A current schema/resource label
disagreement rejects Apply without publishing the draft. Schema-only mock services
can still preview a rename, but execution requires a complete resource projection.

PlanBuilder seals before/after shared-property actions, retains baseline evidence
and label/existence/type footprints, and advances the projected database state.
Later resource actions depend on the schema action that last projected that resource.
This preserves move/rename/schema-edit ordering without a general dependency graph.
Ordinary compatible schema/resource batching remains unchanged.

Materialization retains the projection on the single reviewed schema action.
SchemaHandler validates that it targets the form's database entry, changes only
that entry's label, agrees with both schema pre/post labels, and declares the full
verification footprint. Incomplete projections fail before confirmation/writes.
Direct resource-label patches remain outside the enabled resource-handler contract;
parent updates continue through ResourceHandler.

## Execution, evidence and recovery

Rename confirmation shows the old/new names and the exact single schema request.
Preflight includes both resources. Immediate predecessor checks and read-back cover
the complete schema and the database entry's label, existence and type. Reading
both representations is conservative: the reads are not an atomic transaction.

Observation.related holds immutable secondary observations. Each retains its own
state, receipt/version or structured failure. Journal preimages, postimages,
reconciliation and recovery records retain the complete bundle. Final reports keep
the latest independent observations for normal form/resource tabs, including
explicit unknown state instead of substituting intended values.

Intended and unchanged classifications require both representations to match.
An unreadable secondary representation is unknown. A schema-only match with an
inconsistent database label is divergent and blocks automatic compensation.
Unknown outcomes reconcile both reads before selecting recovery. A concurrent
change to either owned representation blocks compensation; unrelated database
properties/entries are outside rename ownership.

Compensation sends the immediate schema preimage once, restoring the shared label,
and verifies both representations before continuing reverse recovery. No second
resource-label write or repair is guessed. A reported restoration error still
stops recovery even if both restored representations are subsequently observed.
Existing schema-only/data-unverified limits for field deletion remain unchanged.

## Validation

The regression suite covers one-request rename and retained secondary receipts,
no-op rename, stale secondary state, unreadable/mismatched representations, unknown
reconciliation, unchanged-failure RollbackLast selection, concurrent owned-label
changes, preservation of unrelated resource changes, committed write/recovery
errors, recovery verification of both representations, forms/subforms, multiple
renames, pending record labels and mixed move/rename/schema-edit reverse rollback.
The mocked page workflow checks confirmation, execution and pinned report tabs.
Malformed verification projections are rejected before writes.

Full suite: **520 tests pass in 35.252 seconds**, exit 0, no failures or skips.
All 501 stage-7 tests remain; 19 new rename regressions exercise the shared-label
contract. The former gate assertions now validate missing-projection rejection
and successful one-request execution. `git diff --check` is clean.

The checkpoint is recorded in HANDOVER.md. All verification uses the existing
virtual environment and mocked endpoints/services. No dependencies or live API
writes were introduced. The initial migration's remaining rename capability is
now implemented; AddForm/DeleteForm and richer data recovery remain later features.
