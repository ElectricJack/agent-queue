# K14 synthetic deprecation and restore evidence — 2026-10-03

Task `fresh-current.1`; approved implementation plan `rev-quick-orbit` r1, K14.
Implementation started from mainline `fb8b22d3d`, after rebasing the empty assigned
branch from `6c2b29efa`. Supervisor-authorized real merges then incorporated
phase-6 `527ea4dea` (K08/K12/K13, migrations through 65) and the prepared runbook
`94f11b86a`. The final K14 migration is revision 66. This is a synthetic functional rehearsal, not live
reconciliation or a production recovery.

## G7 decision state

**G7 remains closed, awaiting an explicit operator decision.** No announcement,
two-release/30-day window, live complete-source inventory, external adapter
coverage or zero-unsafe-write attestation was supplied for this installation.
No live activation, import, provider calls, uninstall or deletion occurred.
This report and passing tests do not authorize any of them. Separate operator
authorization remains necessary even after G7 for uninstall and deletion.

K11's graph, K07's import and the authorized K08/K12/K13 prerequisites exist on
the final tested branch. Actual provider, quality, cross-harness and security
release evidence remains independently required before their features or the
full replacement are accepted. Merging their commits does not certify delivery
to the default branch or approve activation.

## Exact focused checks

All database tests used `POSTGRES_TEST_DSN` for the separate test service on
localhost:5534. AQ's worker database refusal sentinels were left unchanged.
The test harness allocated and cleaned exclusively owned scratch databases.

```bash
aq test tests/test_knowledge_deprecation.py tests/test_knowledge_compatibility.py \
  tests/test_record_doctor.py -x
aq test tests/test_knowledge_deprecation.py -m migration -x
```

Results on the merged branch: **32 passed** (18.96 seconds) and **1 migration test passed** (11.50
seconds). Initial focused attempts exposed and fixed database-service-interface,
JSON counter serialization and stale erasure-precondition mistakes. The final
checks passed without weakening or deselecting a failing test. One earlier
invocation lacked `POSTGRES_TEST_DSN` and ran nothing; it is not counted.

The restore test executed `pg_dump --no-owner --no-acl` and `psql -X -v
ON_ERROR_STOP=1` using the disposable cluster's matching client tools. It compared
every retained record/knowledge table row and every retained vault file, checked
installation identity and exact historical hashes, retained ownership/counters,
and replayed imports with zero duplicate creations. It captured an erasure
performed after backup and reapplied that request on the restored copy before
delivery, then required typed redacted unavailability. Complete synthetic
reconciliation includes observed vector inventory; missing, quarantined, changed
and unavailable-source cases are also tested and remain closed.

## Other checks

Ruff passed on all changed Python paths. `scripts/check-docs.py` passed for the
three operator guides, design contract and this report. Both API clients were
regenerated after the authorized prerequisite merges. Documentation ownership inventory
checking passed; its pre-existing stale coverage-manifest notice is informational
and the foundation-owned artifacts were not regenerated.

The plan's final noninterference check on the merged branch passed **582 tests** (176.73 seconds):

```bash
aq test tests/test_claim_commands.py tests/test_pool_reconciler.py \
  tests/test_integration_repair.py tests/test_integration_candidates.py \
  tests/test_integration_outbox.py tests/test_task_close_completion_recovery.py \
  tests/test_review_service.py -x
```

Schema invariants passed **24 tests** (17.32 seconds):

```bash
aq test tests/test_migration_single_head.py tests/test_migration_boolean_defaults.py \
  tests/test_migration_string_defaults.py tests/test_migration_json_columns.py \
  tests/test_sqlite_removal.py -x
python3.12 -m alembic heads
```

The head check returned the single head `a00000000066` at the time of the
rehearsal; the K14 migration is `a00000000067` after the re-chain onto main's
`a00000000062_legacy_completion_identity`. The installed standalone
`alembic` executable had a broken shebang; the Python module command succeeded.

The expanded area check passed **760 tests** (96.29 seconds), including the
authorized K08/K12/K13 prerequisites:

```bash
aq test tests/test_record*.py tests/test_knowledge*.py tests/test_notes_plugin.py \
  tests/test_plugin_services.py tests/test_selection_catalogue.py -x
```

All results are retained in the task comments and final close evidence.
The latest recorded full-suite baseline used for comparison was
`full-suite-baseline-2026-09-22.md`; no whole-suite baseline was captured here.
