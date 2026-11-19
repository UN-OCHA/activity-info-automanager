# Stage 0 preservation and refactor baseline

Completed: 2026-11-19. Scope: preservation, baseline establishment, verification and
the migration test inventory. No architecture code, dependencies or live APIs changed.
Stage 1 has not started. See the [specification](execution-refactor-spec.md) and
[implementation plan](execution-refactor-plan.md).

## Preserved implementation

- Original checkout: `C:/Users/Nick Imboden/Documents/activity-info-automanager`,
  still on `master` at `df131da2a18cb14c63a4cb27fdd1f52a9f04d89c`.
- Reference branch: `reference/pre-execution-refactor-20261119`.
- Reference commit: `d1e9e6863f9fbd0ff10d07eb802812e95a8444f8`, directly based on
  `df131da2a18cb14c63a4cb27fdd1f52a9f04d89c`.
- Checkpoint contents: the 15 previously modified tracked files, eight new source/
  test/design files, and all unchanged tracked files. Git uses the repository's
  normal Windows line-ending conversion. The reference was verified against the
  original work using a separate index; the original index was not staged or replaced.
- A separate byte-for-byte copy contains 99 SHA256-verified files: the tracked tree,
  eight non-cache untracked files and the ignored HANDOVER.md. Its manifest, original
  status, staged/unstaged patches and commit ID are kept alongside the copy.
- Preservation directory:
  `C:/Users/Nick Imboden/AppData/Local/JetBrains/PyCharm2026.3/ai-agent-extensions/agents/air-views/adb618f1a2d6405739f53826d4c24855/codex/9916a5fb8a8d51c1/stage0-preservation-20261119`.
- The 104 untracked cache files were left in place and excluded from both the commit
  and preservation copy. No reset, clean, stash, file deletion or original checkout
  switch was used. The existing virtual environment stays in the original checkout.

The checkpoint is the pre-stage-0 source/design snapshot. Subsequent stage-0 status
and inventory edits are carried by the refactor branch and retained as docs in the
original checkout. The original source files remain uncommitted there by design.

### Inventory of formerly uncommitted work

| Category | Files |
| --- | --- |
| Tracked application edits | `activity_manager/api.py`, `command_creator.py`, `command_execution.py`, `command_list.py`, `multi_form.py`, `page.py`, `schema_change.py`, `schema_identity.py`, `schema_names.py`, `schema_resolution.py` |
| Tracked catalog edits | `activity_manager/commands/__init__.py`, `activity_manager/commands/catalog.py`, `test/test_catalogs.py` |
| Tracked documentation edits | `README.md`, `docs/command-iteration.md` |
| New form-command modules | `activity_manager/commands/modify_form.py`, `activity_manager/commands/move_form.py` |
| New folder/adapter modules | `activity_manager/form_folders.py`, `activity_manager/form_move_execution.py` |
| New command test modules | `test/test_modify_form.py`, `test/test_move_form.py` |
| New reviewed design documents | `docs/execution-refactor-spec.md`, `docs/execution-refactor-plan.md` |

## Established migration base

- Worktree: `C:/Users/Nick Imboden/Documents/activity-info-automanager-refactor`.
- Branch: `refactor/execution-state`, created from `df131da`.
- Only design/plan/stage-0 documentation is added to that base. No application or
  test files are ported in stage 0. HANDOVER.md is copied separately and stays ignored.
- Dependencies are reused from the original checkout's `.venv`; none are installed.
  Python imports were checked to resolve `activity_manager` from the new worktree.

Run future baseline checks from the refactor worktree using:

```powershell
& 'C:/Users/Nick Imboden/Documents/activity-info-automanager/.venv/Scripts/python.exe' -B -m unittest discover -s test -t . -q
```

To inspect preserved form-command work without changing either checkout:

```powershell
git show reference/pre-execution-refactor-20261119:activity_manager/commands/modify_form.py
git show reference/pre-execution-refactor-20261119:test/test_move_form.py
git diff df131da reference/pre-execution-refactor-20261119 -- activity_manager test
```

## Verification results

Both runs used the same existing Python 3.14 environment and mocked API services.
No failures or skips were reported. Asyncio emitted slow-task diagnostics; both
processes exited successfully.

| State | Tests | Result | Suite time |
| --- | ---: | --- | ---: |
| Original current implementation, matching the reference checkpoint | 393 | Passed | 25.621 seconds |
| Isolated `df131da` migration base | 356 | Passed | 12.887 seconds |

The 37-test difference is exactly 16 ModifyForm tests plus 21 MoveForm tests.
Existing module test names are retained in the base. `test_catalogs.py` has the
same four tests but adds expectations for the two new commands in the reference;
those expectations must return with their stage-6 command ports. Passing 356 tests
does not establish that ModifyForm/MoveForm have migrated.

## Test inventory and migration expectations

Preserve the existing behavioral suites; do not replace them with tests that only
assert new class structure. Counts below are discovered tests in the current tree.

| Suites | Current tests | Migration use |
| --- | ---: | --- |
| `test_add_field.py` | 11 | Transformations, deferred identities, generated writes |
| `test_app.py` | 33 | API, parsing, schema comparison, recovery and UI contracts |
| `test_catalogs.py` | 4 | Catalog integrity; restore form-command expectations in stage 6 |
| `test_command_card.py`, `test_command_warnings.py` | 11 | Cards, counters, status and warning presentation |
| `test_command_execution.py` | 7 | Diagnostics and application contracts; tighten resolver atomicity |
| `test_command_iteration.py`, `test_form_iteration.py` | 50 | Frozen selectors, substitutions, exact order, scope variables, counts and histories |
| `test_multi_form.py` | 22 | Revisit/local state, tabs, ordered writes, preflight and rollback |
| `test_field_applicability.py`, `test_field_parameters.py` | 30 | Existing creator/parameter applicability contracts |
| `test_form_names.py`, `test_form_tree.py` | 11 | ID/name resolution and related-form context |
| `test_modify_field.py`, `test_modify_field_execution.py`, `test_modify_field_e2e.py` | 48 | Atomic field edits, identities, option/lookup handling, generated payloads |
| `test_modify_form.py` | 16 | Retain in reference; port all in stage 6 |
| `test_move_form.py` | 21 | Retain in reference; port with intentional order/recovery changes in stage 6 |
| `test_parameter_functions.py`, `test_append_formula.py`, `test_append_function.py`, `test_regex_replace_function.py`, `test_replace_function.py` | 49 | Existing function semantics and restrictions |
| `test_formula_parser.py` | 7 | Formula grammar behavior |
| `test_pending_identity_allocation.py` | 5 | Reserved identity namespaces and collision handling |
| `test_default_comparison.py`, `test_schema_write_names.py` | 19 | Schema equivalence, materialization, formula literals and canonical defaults |
| `test_schema_display.py`, `test_schema_names.py` | 18 | Readable values and rendering context |
| `test_preview_resolution.py`, `test_strict_resolution.py` | 17 | Required reference/default resolution, async freshness and write gating |
| `test_set_variable.py` | 14 | Literal/lookup values, captured results and variable scope |
| **Total** | **393** | **356 base + 37 preserved form-command tests** |

### Intentional behavior changes and additional acceptance cases

| Contract | Existing reference / gap | Required migration expectation | Stage |
| --- | --- | --- | --- |
| Snapshot context | `test_form_iteration.py` and `test_multi_form.py` cover historical tabs/annotations; some context comes from later caches | Related schema/name context and pinned labels survive subsequent cache/state changes | 1, 6 |
| Resolution atomicity | `test_preview_resolution.py` can inspect an invalid transformed candidate; diagnostics stacking exists in `test_command_execution.py` | Published state, variables and plan retain the prior successful state; candidate paths stay on a diagnostic attachment | 2 |
| Multi-form grouping | Command-first application and accumulated A/B writes already characterized | Preserve this batching; no general cross-form dependency guard or splitting requirement | 2, 3 |
| Persisted deletion boundary | DeleteField is currently folded into accumulated schema replacements | One action-start field deletion per action; later edits use a new action; temporary IDs alone do not imply same-action creation | 3, 5 |
| Materialization/confirmation | Existing identity suites protect IDs and literals; allocation currently follows confirmation | Allocate once before confirmation, freeze exact action payloads and invalidate changed/cancelled attempts | 4 |
| RollbackLast after unchanged failure | `SequenceTests.test_rejected_write_offers_full_rollback` covers rejection with RollbackAll, not last-changed selection | Skip proven unchanged attempts and select the prior changed action; do not skip unknown/divergent unsafe entries | 5 |
| Divergent recovery | Legacy execution can compensate against any observed postimage; verification-read recovery already covered | A matching reread of divergent state does not establish ownership; block unsafe compensation and earlier rollback | 5 |
| Mixed move/schema order | `MoveFormTests.test_single_move_switches_context_and_keeps_schema_clean` asserts schema-before-move; mixed rollback tests use that order | Move before subsequent schema edits; reverse that actual order during recovery; adjust rollback-last expectations explicitly | 6 |
| Patch batching | Current adapter handles one form location per step | Compatible adjacent moves may share a request and complete rollback unit; owned-property checks preserve unrelated entries | 6 |
| Rename | Preview tests cover local renames; server resource-label side effects remain unestablished | Verify both projections with endpoint evidence, retain receipts/versions, and gate unresolved rename execution | 4, 6 |
| Schema-only field recovery | No verified deleted-field data recovery | Confirmation/reports state the limitation; schema equality never claims restored record data | 4, 5 |
| Final reports and browser flow | Existing schema/move page tests cover reporting | Mixed resources, unknown observations, confirmation invalidation and reevaluation retain truthful stable reports | 5-7 |

Future AddForm/DeleteForm and recovery-endpoint experiments remain outside these
initial migration checks. The next work is stage 1 only after the user's instruction.
