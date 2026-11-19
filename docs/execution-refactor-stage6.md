# Stage 6: form commands and canonical resource execution

Subsequent update: the user clarified the shared-label contract and authorized
[rename execution](execution-refactor-rename.md), which is now implemented. The
gate described below records the state at the stage-6 checkpoint.

Implemented 2026-11-19 in `refactor/execution-state` following explicit stage-6
authorization. Supported porting work is complete; **rename execution remains an
explicit incomplete capability** because its endpoint side effects are unverified.
Stage 7 has not started. Do not call the whole migration complete with this gate.

## Commands and application

Ported preserved ModifyForm and MoveForm parsers, catalog/creator definitions,
single/multiple targeting, optional edits, folder resolution and suggestion guards.
The Applier reuses its atomic form-switch/scope publication for both commands.
Pattern targets and form captures remain frozen, variables are discarded on scope
replacement, and local names override cached remote directories. Failed selected
form edits now retain the preceding published context, per the stage-2 atomicity
contract; rejected candidate data stays diagnostic only.

ModifyForm supports optional schema name/record-label edits, ID-first/code field
matching and blank record-label clearing. Pending record-label IDs use the existing
confirmed materialization map. Rename previews project the resource and schema
labels together when resource loading is supported. Required resource-read failures
reject publication. Old schema-only test services retain their preview compatibility.

MoveForm resolves database-scoped ID/name/path destinations and root `/`, permits
top-level forms only, and emits ResourcePatchEffect against canonical database
resources. It never changes a schema payload or maintains parallel location arrays.
Explicit destination prerequisites and owned parent properties are retained in the
plan. Compatible adjacent entries batch into one action; schema operations and
context boundaries retain the planner's ordering rules. Moves precede subsequent
schema edits. Apply and suggestions perform reads only.

## Adapter, materialization and executor

ActivityInfoService loads canonical database snapshots with available versions and
encodes resourceUpdates using the installed generated UpdateDatabaseRequest and
update_database endpoint. Raw/parsed receipts and versions use the stage-4 evidence
boundary. A missing parent on a form/folder is normalized specifically to its
database-root identity; other resource properties retain strict presence/type
semantics. The immutable receipt still preserves the server response.

Confirmation materializes schema identities and retains resource action snapshots
in their actual order. The page presents resource destinations and exact request
payloads. ResourceHandler verifies owned properties, entry/destination existence
and destination type, without comparing unrelated entries/properties. Preflight
includes these resource requirements before any write; immediate checks revalidate
them before the action. Observations include complete resource collections for
historical display, while ownership comparisons remain limited to declared paths.

Compensation patches only immediate prior owned values. Unrelated concurrent
labels/permissions survive. Only parentId updates of existing resources have an
enabled write contract; other properties, removal/clearing, creation and deletion
remain gated. Partial application of a batched patch is divergent and blocks
automatic recovery. Mixed recovery reverses verified schema actions and then
resource actions, preserving the shared immutable journal, receipts and report.

DisplayState exposes independent database-resource tabs through its UI adapter.
Resource snapshots retain their own entries/order/properties across later moves;
changed-resource tabs are preferred for moves, and subsequent schema edits prefer
their form tab. Selection does not change application context. Schema-only legacy
display consumers retain legacy_forms; the page uses legacy_tabs. Report snapshots
render either payload kind and remain pinned through cache clearing/reevaluation.

No form_move_execution.py adapter was introduced into this worktree. Its preserved
original copy remains untouched; the active runtime has no caller or synthetic
schema metadata/location sidecars. Legacy schema visits remain for stage-7 cleanup.

## Rename evidence and unresolved capability

Read the installed client 2.0.4 schema/database request and response models and
generated endpoints, and inspected these official references on 2026-11-19:

- [Update form schema](https://www.activityinfo.org/support/docs/api/reference/updateFormSchema.html)
  accepts a schema label and returns database/form information.
- [Update database](https://www.activityinfo.org/support/docs/api/reference/updateDatabase.html)
  exposes separate resource label and parent updates.
- [Renaming a form](https://www.activityinfo.org/support/docs/maintenance/renaming-a-form.html)
  describes the user workflow through the form designer, without specifying which
  requests establish consistency between schema and resource labels.

These sources establish request shapes but do not establish whether a schema label
write updates the resource label, whether a separate resource-label write affects
the schema, or the required compensation order if two writes are necessary. A
generated response model is not proof of those server side effects. No live
experiment is authorized. Consequently schema rename actions and resource label
patches fail preparation before confirmation/writes. The verification footprint for
an eventual rename must cover both labels; neither one-label equality nor the UI
instructions are accepted as proof. Record-label-only edits remain executable.

## Validation and intentional changes

Full suite: **499 tests pass in 29.523 seconds**, exit 0, no failures or skips.
This includes all 456 stage-5 tests, all 37 preserved form-command tests ported to
the new contracts, and six new resource-safety tests. The preserved tests now use
canonical resource snapshots and the shared Executor, rather than the old folder
adapter. Original source/tests are untouched in the reference checkout.

Intentional expectation changes explicitly covered: atomic failure retains the
previous form/context; pending record labels materialize before confirmation;
moves execute before later schema edits; compatible moves batch; destination
removal fails preflight before any write; historical folder state lives in resource
tabs; partial batched outcomes block recovery; rollback reverses actual action order.
Tests also cover generated parent-only payloads, unrelated concurrent edits,
destination type changes, owned-property compensation, rename projection/gating,
scope replacement, captures, ambiguity/subforms, stale suggestion responses and
mixed reverse rollback. Catalog expectations include both new commands.

Used the existing virtual environment with `-B`, mocked services/endpoints and no
new packages, live API writes or recovery experiments. `git diff --check` is clean.
