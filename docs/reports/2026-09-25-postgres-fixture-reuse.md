# PostgreSQL fixture reuse — wise-glacier

## Change

`reuse_database` initializes each lease database once for the pytest worker's
session. The existing template-cloned lease pool still resets rows,
identity sequences, and migration seeds after every test. The fixture closes
connections on the test's event loop before that reset. Each test gets a fresh
adapter and engine, preserving pool configuration and isolating callbacks and
asyncio locks. Repeated factory requests for a name within a test share its adapter.

Provider usage now runs its 41 query tests once on PostgreSQL, instead of 82
cases with `sqlite` and `postgres` labels for the same backend. This also
eliminates its per-test `reset_for_tests()` loop, which truncated every table
separately. An identical shadowed test definition was removed; the collected
test's assertions remain.

The shared fixture is opt-in. Other tests retain their initialization behavior.
See [the fixture guide](../contributing/testing.md#reusing-a-database-in-data-tests).

## Fixture audit

All 40 `tests/test_integration_*.py` modules were inspected. The 32 ordinary
database fixtures below now use the cache; their project/repository/task seeds
remain scoped to each test. Adapter setup and cleanup are owned by the factory.

| Module suffix (`test_integration_…`) | Fixture |
| --- | --- |
| attestation | attestation_db |
| candidates | db |
| child_delivery | case |
| ci | ci_db |
| cleanup | release_db |
| controls | db |
| delegate_release | db |
| eject | db |
| finished_owners | db |
| hierarchy | db |
| identity_rebind | env |
| legacy_deliveries | env |
| legacy_repositories | db |
| main_promotion | prepared_db, ordinary lease branch |
| operational_controls | db |
| operator_controls | db |
| outbox | db |
| owner_recovery | env |
| ownership | db |
| parent_completion | db |
| promotion | db |
| repair | db |
| review_evidence | review_case |
| root_pull_requests | db |
| schedule | db |
| sealing | db, concurrent_db (separate leases) |
| service | db |
| settling | db |
| stale_owners | env |
| stale_schedule | db |
| state | db |

Explicit initialization remains for:

- `test_integration_mode.py::orch`: tests the orchestrator's full lifecycle.
- `test_integration_main_promotion.py::prepared_db[postgres]`: a dedicated scratch
  database and one-connection pool for atomic finalization coverage.
- `test_integration_cleanup.py::test_postgres_last_cleanup_items_serialize_aggregate_projection`:
  dedicated concurrent adapters and a scratch database.
- `test_integration_schedule.py::test_not_due_and_restart_duplicate_delivery_are_durable`:
  closes and reopens the adapter to check persistence across restart.
- `test_integration_state.py::test_baseline_creates_every_integration_table`:
  explicit current-schema setup coverage.
- The standalone data tests in `test_integration_hierarchy.py` and
  `test_integration_main_promotion.py`: one-off setup, outside the repeated fixtures.

No schema, production adapter, migration, or scheduler behavior was changed.

## Timing method

The original fixtures were saved from `f76fa685d` into a temporary
`tests/_wise_glacier_baseline/` directory. Both samples ran on source base
`7c74ef351` after the duplicate migration repair (#639), with the same disposable
PostgreSQL 18 server on port 5534, serial execution, and default marker selection.
The temporary fixtures were removed after the run.

**Test-seconds** are the sum of JUnit testcase `time` values, including setup,
call, and teardown. Suite wall time is reported separately. These are measurements
on the shared development box, not a complete CI-job benchmark.

Before:

```bash
POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue_test:agent_queue_test_dev@localhost:5534/postgres \
  aq test tests/_wise_glacier_baseline/ -p no:xdist --durations=0 \
  --junitxml=/tmp/wise-glacier-before.xml
```

After:

```bash
POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue_test:agent_queue_test_dev@localhost:5534/postgres \
  aq test tests/test_integration_ownership.py tests/test_integration_service.py \
  tests/test_integration_state.py tests/test_provider_usage_queries.py \
  -p no:xdist --durations=0 --junitxml=/tmp/wise-glacier-after.xml
```

## Paired results

| Module | Before cases | After cases | Before test-seconds | After test-seconds |
| --- | ---: | ---: | ---: | ---: |
| provider_usage_queries | 82 | 41 | 97.127 | 13.889 |
| integration_ownership | 15 | 15 | 6.553 | 5.980 |
| integration_service | 18 | 18 | 3.312 | 2.507 |
| integration_state | 18 | 18 | 9.839 | 5.763 |
| **Total** | **133** | **92** | **116.831** | **28.139** |

Both runs passed. Pytest wall time fell from **117.98s** to **29.78s** (74.8%);
summed test time fell **75.9%**. Provider usage alone fell **85.7%**.

The integration-only total fell from **19.704s** to **14.250s** (27.7%). The
rounded duration reports show integration setup falling from **5.37s** to
**2.57s**, including lease-pool provisioning; `integration_service` setup fell
from **0.61s** to **0.01s**. Integration call time fell from **3.60s** to
**2.68s**, and teardown from **10.68s** to **8.98s**. These runs did not isolate
machine load, so the overall integration change cannot be attributed entirely
to initialization reuse. The fixture lifetime regression test establishes
initialization reuse independently of wall time.

## Verification

The focused hierarchy and substrate gate passed **94 tests in 85.27s**:

```bash
POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue_test:agent_queue_test_dev@localhost:5534/postgres \
  aq test tests/test_postgres_test_substrate.py tests/test_integration_hierarchy.py \
  --junitxml=/tmp/wise-glacier-focused.xml
```

The substrate regressions verify one initialization across tests and event
loops, clean rows, restored migration seeds, independent concurrent leases,
fresh callbacks/configuration/locks, and `NullPool` on a previously initialized
database. The full hierarchy module covers a CLI/API call crossing loops after
earlier tests initialize the lease. The initial adapter/engine cache failed
that existing CLI test; retaining only database initialization and creating
fresh adapters/engines corrected it.

The final area gate passed **1,388 tests in 284.53s (4m44s)** at the normal
four-worker cap:

```bash
POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue_test:agent_queue_test_dev@localhost:5534/postgres \
  aq test tests/test_provider_usage_queries.py tests/test_postgres_test_substrate.py \
  tests/test_integration_*.py --junitxml=/tmp/wise-glacier-final-area.xml
```

Ruff on changed Python files, `git diff --check`, and documentation ownership
(`refresh_inventory.py --check`) passed. The migration repair was obtained from
main; this task makes no migration changes. `python -m alembic heads` reports
one head, `a00000000025`.

## Supplied profiling data

The task's `ci-durations/local-default-n8.txt` contains **539.76** provider usage
test-seconds: **523.19** setup, **3.10** call, and **13.47** teardown. The direct
`postgres` branch contributed **519.31** seconds (including **517.03** setup);
the `sqlite`-labelled PostgreSQL lease branch contributed **20.45** seconds.
The integration modules together accounted for **1,580.09** test-seconds.

Those historical values are context; the paired local comparison above is the
before/after measurement. This change alone does not establish the task's goal
of every complete CI job finishing in five minutes. In particular, dedicated
scratch database finalization and the stateful CLI smoke tests remain separate
costs.
