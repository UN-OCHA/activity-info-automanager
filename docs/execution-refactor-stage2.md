# Stage 2 Applier extraction and atomic application

Completed: 2026-11-19, on refactor/execution-state. Stage 3 has not started.
See the [specification](execution-refactor-spec.md) and [plan](execution-refactor-plan.md).

## Evaluation ownership

applier.py now owns synchronous/local evaluation, asynchronous connected evaluation,
field expansion, form-scope target freezing, per-form variable maps, context changes,
snapshot capture and ordered application results. It has no NiceGUI or command-list
manager imports and creates no UI controls. evaluate_local() runs local transformations;
await evaluate() uses the supplied resolver/read callbacks for connected evaluation.
Both expose immutable authored results through results.

CommandListManager retains authored list editing, stable authored identities, text
editing, selection/expansion, busy controls, task cancellation and UI refresh. It
constructs an Applier for each evaluation and renders progress from the current
engine. Its workspace/cache/result accessors are temporary compatibility adapters,
not application loops. CommandExecution moved into command_execution.py and remains
importable from command_list.py for existing consumers.

Authored commands are copied at evaluation entry. Existing PreparedCommand,
single-pass substitution, function and field transformation code is reused.

## Atomic publication

Each actual application receives a private ApplicationDraft containing its workspace,
canonical state, schema visits, annotations, related context, database names, reference
defaults, reserved identities, variables, snapshot builder and staged effects.

Transformation, required resolution, immutable value validation and snapshot capture
finish before publication. Only success or warnings publish the candidate workspace,
context, variables, reserved identities, snapshots and effects. Errors and raised
exceptions preserve the preceding canonical resource revisions, active context,
schema visits, annotations and variables. A failed B application after a successful
A application retains A's reached state and still records B as the failed logical target.

Publication also detaches the committed values from draft references retained by a
callback. Later mutations through such references cannot alter published workspace,
context, variables or snapshots. Publication copies are prepared before assignment.

The Applier's schema effects are immutable application-ID-linked before/after resource
revisions. They are emitted only for changed schemas, after successful application.
Context-only commands and variable assignments generate no schema writes/effects.
If snapshot capture fails after staging an effect, the draft effect is discarded.
These are application effects, not a compiled API plan; PlanBuilder is stage 3.

ApplicationState also exposes frozen global/scoped variable maps, active scope and
reserved identity views. Scope exit cleanup is committed only when the new context
succeeds. Frozen multi-form targets, full known application counts, flat field/form
children, zero-match parents and local-over-remote resolution are retained.

## Resolution callbacks and cancellation

ACTIVE_DRAFT supplies candidate context to existing resolver callbacks that access
workspace/related/cache properties through CommandListManager. This avoids temporarily
replacing published manager fields while a callback awaits remote reads. Old tasks
retain their own draft context even after the manager adopts a new Applier.

A cancelled or superseded engine checks currency before staging/publishing and
before reporting progress. Resolver callbacks that catch cancellation and later
return cannot publish into the new evaluation. Each evaluation owns a separate
read-cache dictionary. Within one evaluation, successful read-cache enrichment may
survive an application failure; logical reference-default/state changes do not.

The supplied service/resolver/lookup callbacks retain their existing read-only role
during Apply. No API write endpoints or execution/recovery behavior were added.

## Rejected candidates and intentional behavior changes

Failed candidates and candidate-relative diagnostics are retained in immutable
DiagnosticAttachment values, with captured readable values where available. The
published schema and its annotations remain the preceding successful values.

The UI offers a separate expansion labelled "Rejected candidate (not applied)".
It displays the draft and highlights its paths; those paths do not annotate the main
successful-state schema. The console retains all warning/error messages.

Two existing tests were deliberately updated: unresolved reference/default errors and
combined formula-warning/default-error handling now inspect candidate annotations on
the attachment and assert that published state stays unchanged. Their diagnostic
coverage was retained. Warning-only behavior still publishes successful changes.

Low-level command/resolver helpers continue to return staged candidates. The Applier
is the boundary that commits or rejects them; callers should not treat helper output
as published application state.

## Validation and stage boundary

The full suite passes: **386 tests in 19.028 seconds**, exit code 0, no failures or
skips. That is the stage-1 suite of 373 tests plus 13 tests in test_applier.py.
A separate fresh-process headless import/application check also passed.

New tests cover local evaluation without UI construction; returned/raised transformation
failure; returned/raised resolution failure; variable, reserved-ID and context rollback;
read-cache retention; staged-effect disposal on snapshot failure; single-pass escaping;
failed scope replacement; failed multi-form targets; cancellation-ignoring resolvers;
stale legacy callbacks; retained draft references; and separate rejected-candidate UI highlighting.

Existing parser, creator, iteration/variables, snapshot/history, annotation, API mock,
execution and recovery tests pass. The original checkout's application code remains
unchanged. Tests used its existing virtual environment; no dependencies were installed
and no live writes or recovery experiments were run.

Next is stage 3: PlanBuilder, explicit groups/boundaries, creator/action-start provenance
and isolated persisted field deletion actions. The legacy ordered schema-visit executor
is still in use. No general cross-form dependency analyzer was introduced.
