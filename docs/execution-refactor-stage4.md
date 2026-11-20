# Stage 4: handlers, adapter evidence and frozen confirmation

Completed 2026-11-19 in `refactor/execution-state`. The user authorized stage 4.
Stage 5 has not started.

## Delivered contracts

`endpoint_evidence.py` defines immutable observations, failures, HTTP receipts and
write evidence. Missing (404/410), forbidden (401/403), unavailable transport/server
responses, rejected requests and invalid responses remain distinct. Only explicit
not-found evidence establishes absence. Receipts retain status, headers, raw body,
parsed body where available, and returned schema/database versions. A successful
write receipt does not establish that the intended state was applied.

`ActivityInfoService.observe_resource` reads schemas or database resources into
canonical state, validating resource identity. `write_schema_evidence` accepts an
already materialized schema without name resolution, queries or ID allocation.
The adapter uses installed activity-info-api-client 2.0.4 generated `_get_kwargs`
and `_parse_response` helpers around its session HTTP client. This keeps the
response before generated parsing: `asyncio_detailed` parses before returning
and otherwise loses receipts when parsing fails. Paths, SDK encoding and decoding
remain in the adapter. Legacy adapter methods are unchanged. These private
generated helpers are an explicit dependency to revisit on client upgrades.

`action_handlers.py` supplies the preparation/read/compare/write protocol and schema
and persisted-field-deletion handlers. They reuse existing schema equivalence;
deletion preparation validates its identified removed field. Resource footprint
comparison treats property presence, null, false and scalar types independently,
ignoring unrelated entries when only an owned property is requested. Rename
encoding is blocked pending the stage-6 endpoint side-effect evidence gate.

`confirmed_plan.py` materializes active actions locally before confirmation. Shared
form-scoped mappings preserve fields, options, lookup rows, metadata IDs, record
labels, reverse/translation references, formula identifiers and action/member
provenance. Existing formula walkers retain literal text and cross-form identifier
context. Omitted actions allocate no IDs. Preview plans and historical state remain
immutable. Comparison schemas and reference-label context are detached and pinned.
The metadata allocator now retains fixed metadata identities in the shared map and
permits their reuse across snapshots without treating them as new collisions.

ExecutionPlan contains the exact ordered actions and request snapshots, effects,
contributors, subjects/targets, deletions, recovery limits, baselines and ID map.
ConfirmationAttempt binds it to evaluation, source and session identity.
Confirmation names its attempt ID; cancellation, explicit invalidation, a changed
candidate or changed binding permanently invalidates that attempt. The executor
entry point must call `require_confirmed(current_binding, current_candidate)`;
confirmation/retry never reruns materialization. Request encoding performs no
execution-time substitutions or online queries.

## Runtime boundaries

This is an unwired contract layer. The existing UI and legacy ordered schema
execution remain active. Stage 5 must add all-baseline preflight, immediate
predecessor checks, journal/outcome classification, verified recovery and the UI
cutover together. No new Execute route has been exposed.

Resource footprint comparison/read evidence is available, but materializing a
database patch explicitly rejects it until stage 6 supplies request encoding and
MoveForm. Schema confirmation therefore has no destinations; its review contract
reserves that dimension for resource actions. No new form lifecycle handlers,
recovery endpoints or general dependency analyzer were added. User testing
subsequently confirmed deleted-field data restoration; former limitation notices
have been removed. Rename
remains blocked. No live writes, recovery experiments or packages were introduced.

## Validation

Full suite: **428 tests pass in 24.258 seconds**, exit 0, no failures or skips.
This retains all 408 stage-3 tests plus 20 tests in `test_confirmed_plan.py` and
`test_action_handlers.py`. Coverage includes generated request payload equality,
receipts on malformed/valid responses, HTTP/transport/identity failures, versions,
property/existence comparisons, stable allocation across actions/forms, deletion
provenance, metadata, options/defaults/translation, record labels, cross-form
formulas and literals, immutable confirmation/context, binding/candidate changes,
cancel, and zero endpoint calls during preparation.

Used the existing project environment with `-B`; endpoints were mocked. The
preserved original/reference suite remains the separately recorded 393 tests;
its 37 ModifyForm/MoveForm tests still await stage-6 ports.
