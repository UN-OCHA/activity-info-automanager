# Stage 5: confirmed execution and verified recovery

Completed 2026-11-19 on `refactor/execution-state`. Stage 5 was explicitly
authorized; stage 6 awaits instruction.

## Execution and journal

`executor.py` consumes exactly one confirmed, materialized ExecutionPlan.
`ConfirmationAttempt.begin_execution` prevents replay, including through a second
Executor instance. The page supplies current evaluation/source/session/candidate
context again at entry and immediately before each forward write.

Every required baseline is read before the first write. A missing baseline is a
blocking error, never inferred from an action's projected predecessor. Schema
equivalence ignores server-version churn using existing comparison rules. Explicit
member prerequisites are checked without a general cross-form dependency scan.
Before each action the executor checks its immediate predecessor; semantically
unchanged actions are recorded as skipped without writes.

JournalEntry is published as attempted before calling the endpoint, retaining the
immutable reviewed request, immediate observed preimage and timestamp. Read-back
runs after successful and failed calls. Forward evidence, returned versions and
receipts remain separate from observations and recovery history. Outcomes are
intended, unchanged, divergent or unknown. Both an intended read-back and absence
of a reported write error are required to continue. A reported error after commit
therefore stops and offers recovery against the known intended postimage.

ExecutionReport retains the frozen plan, journal, preflight, latest observations,
messages and per-action statuses, including not_started and skipped. Unreadable
actual state is explicit and omitted from actual_schemas; intended payloads never
fill it. Detached dictionaries for rendering cannot alter the report. Interrupted
endpoint tasks retain attempted-write/read-back evidence and stop without starting
automatic recovery. An interrupted recovery decision leaves the evidence intact.

## Recovery policy

Leave sends no compensation requests. RollbackLast selects the latest attempted
action with a known or unresolved possible change, skipping unchanged/skipped
actions. RollbackAll walks the applicable entries in reverse action order.

Unknown outcomes are reconciled by reading before compensation. A read proving
unchanged permits selection of an earlier changed action. Unavailable or divergent
reconciliation blocks recovery; it never silently skips to earlier entries.
Originally divergent outcomes remain blocked even if a reread matches the divergent
postimage: observing that state does not establish ownership.

Before compensation, a fresh read must match the known owned postimage. Schema
handlers build the inverse request from the immediate observed preimage. Recovery
attempts retain their exact requests, observations, receipts/errors and timestamps
without rewriting forward outcomes. Every request is read back and verified before
continuing. A recovery error stops automatic recovery even if read-back establishes
restoration; wrong-state success and unreadable verification also stop.

Persisted-field deletion keeps schema-only compensation with record-data restoration
unverified, in both confirmation and reports. Its successful recovery has a distinct
schema_restored_data_unverified status. No new recovery endpoint or data-restoration
guarantee was added. Read-check-write remains optimistic: the endpoint does not
provide conditional writes that close the concurrency window.

## UI cutover and retained code

`page.py` now prepares ConfirmationAttempt before opening confirmation, presents
the ordered targets, contributing command positions, identified deletions and
recovery limits, and exposes the exact materialized request snapshots for review.
Cancel and source changes discard the attempt; candidate/binding changes reject
confirmation before endpoint reads or writes. Duplicate confirmation cannot replay
an attempt. Editing/source controls remain disabled during execution/recovery.

The page invokes Executor, retains `command_list.execution_report` and the runtime
executor/journal for inspection, and refreshes working state from actual observations.
Read caches are invalidated separately. Reports use immutable DisplayState snapshots
with pinned schema/name/record-label context, including when later reevaluation
changes working state. Historical report rendering does not launch name queries.

The old schema-sequence path remains in its module for legacy tests/reference
compatibility; the page no longer calls it. No run sends writes through both paths
or mixes their journals. Legacy visit/planning helpers remain for stage-7 retirement.
Stage 6 still owns ModifyForm/MoveForm, resource request/recovery handlers and tabs.
Resource patch materialization and unresolved rename side effects remain gated.
AddForm/DeleteForm and richer recovery remain separate later features.

## Validation

Full suite: **456 tests pass in 110.835 seconds**, exit 0, no failures or skips.
The final focused executor/page run passes **28 tests in 1.406 seconds** after the
recovery-request audit fields/signature were finalized. `git diff --check` is clean.

New tests: 23 in `test_executor.py`, 5 in `test_executor_page.py`. They cover
all-baseline preflight, missing/unavailable/stale baselines, immediate checks,
attempt-before-write journaling, exact reviewed payloads, revisits/preimages,
no-op skips, before/after-commit errors, divergent success, unknown reconciliation,
unchanged rollback-last selection, concurrent edits, recovery errors/read-back,
wrong-state/unreadable recovery, schema-only deletion recovery, immutable reports,
single-use confirmation, context changes, interruption, cache invalidation,
cancel/edit/source invalidation and all three recovery choices through mocked pages.

All 428 stage-4 tests remain. The original/reference 393 tests remain a separate
stage-0 result; 37 preserved form-command tests still await stage-6 porting.
Used the existing virtual environment with `-B`, mocked endpoints/services, and no
new packages, live API writes or recovery experiments.
