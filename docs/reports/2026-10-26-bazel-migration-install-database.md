# Migration, installation and database dependency slice

Task: `sharp-flare-92`. Final source base: `origin/dev` at
`512996c4738a348ac2a6372697f184e20a955a8c`, including the shared V2 analyzer
and lazy database prerequisites. This supersedes the earlier blocked checkpoint.
CI selection and execution policy are unchanged.

## Changes

All 100 slice modules now use the per-test dependency generator: 54 migration,
18 installation, and 28 database/db/PostgreSQL workflow modules. Existing fine
grained selections from other slices remain enabled.

`src.install` defers its public exports until requested. Independent comparison
with the original facade confirms all 103 public names and their order;
regressions check export identities, star import, `dir()` and unknown attributes.
Direct platform imports avoid loading installer adapters and daemon services.

Root fixtures resolve the base DSN through `tests.pg_dsn`; the lease forwarding
helper imports the pool only when called. Tests verify that loading conftest
avoids the pool import and that forwarding still uses the current pool.

Seven explicitly declared pure targets have passing pure-mode checks: the two
installer adapter leaves, boolean-default scanning, JSON-column scanning,
session-hook scanning, Alembic revision-head inspection, and PostgreSQL DDL
compilation. Real migration execution retains PostgreSQL setup.

The additive declaration in `scripts/bazel/test_dependency_inputs.json` repairs
Alembic's dynamic `ScriptDirectory` inputs for the single-head test. It includes
`alembic.ini`, every revision, and the integration-guard module imported by a
revision. Independent top-level AST inspection of all revisions finds that one
repository import; function-local upgrade imports are not executed by head
inspection. No shared analyzer overlay is used.

## Collection and workflow retention

Fresh successful collection contains 39,238 unique node IDs. All 39,235 nodes
from the exact dev base remain, with identical markers. Three regressions are
added: lease forwarding, installer export compatibility, and isolated platform
import. The slice retains all 2,085 old nodes and adds those three.

For all 1,013 modules, independent comparison preserves original nondefault
workflow memberships, multiplicities and target labels, plus the combined
coverage of all eight default shards. Default partition assignments may change
when nodes are added. The 54 migration modules retain their original 212 nodes.
Independent collection and the generated manifest have identical node sets.

## Actual Bazel query proofs

Pinned Bazel 8.3.1 was verified against `tools/bazel/toolchains.lock.json`.
Queries used one shared server with `--host_jvm_args=-Xmx2g`; no test builds were
run. Let M be the independently audited real dependency set, and A the actual
Bazel reverse dependencies, restricted to the named module universe.

| Changed input | Universe | Required M | Actual A | Result |
| --- | --- | ---: | ---: | --- |
| `src/install/platform.py` | two pure adapter modules | 1 | 1 | Only platform test |
| `src/install/node_toolchain.py` | two pure adapter modules | 1 | 1 | Only node-toolchain test |
| `src/install/platform.py` | all 18 installation modules | 17 | 17 | Excludes pure node-toolchain test |
| `src/install/node_toolchain.py` | all 18 installation modules | 17 | 17 | Excludes pure platform test |
| `src/database/migration_guard.py` | 28 database/PostgreSQL modules | 28 | 28 | No missing consumers |
| `src/database/dsn.py` | 28 database/PostgreSQL modules | 28 | 28 | No missing consumers |
| `migrations/versions/a00000000121_unlimited_repair_deadlines.py` | 54 migration modules | 53 | 54 | One conservative extra |
| `migrations/env.py` | 54 migration modules | 49 | 54 | Five conservative extras |

Every query exited zero and satisfies M ⊆ A. The installer audit additionally
retains all 199 direct repository-import obligations. Both pure adapter targets
have 19 source inputs and no conservative fallback. The other 16 installer
modules retain actual application/plugin dependencies and remain conservative.

All database modules load root conftest, whose production-database guard imports
migration diagnostics and the DSN helper, independently justifying M=28. Those
modules retain their conservative plugin closure: 1,223 production sources,
all 139 migration Python files, and `alembic.ini`.

For migration revisions, 49 modules retain the autouse database fixture and its
schema-key hashing, three pure scanners read revisions, and the single-head test
loads their graph. The pure DDL compiler does not read revisions. Only the 49
runtime modules require `env.py`. Actual `somepath` queries prove the extra
selections follow `test target -> shared_inputs_* -> //:python_repository_data
-> migration file`: the existing common data group retains `migrations/**` for
every test. The per-test manifest lists exactly M; the rendered graph retains
these extra shared inputs. This is retention evidence, not migration narrowing.

Graph bindings during every query:

- Manifest SHA-256: `33d16b3e9b4ffc19d17ca49b37eb3b58c475469bd8ae600456dfb2d100dd3f67`.
- Test BUILD SHA-256: `6f839ed5f261b332c962d18283181385d329423579f167ad4699ed0eeeabc8bf`.
- `tests/BUILD.bazel`: 18,375,761 bytes; manifest: 22,345,905 bytes.

No mass removal of declarations is inferred from line counts alone: all nodes
and workflow obligations survive, independently audited inputs are retained,
and actual query results preserve M. The BUILD reduction reflects V2 factoring
and removal of unrelated leaf inputs.

## Validation

All execution uses AQ admission and the configured worker cap. Database tests
use the disposable PostgreSQL service on port 5534, with worker refusal variables
unchanged.

| Current-source check | Result |
| --- | --- |
| Focused changed installer/database/pure migration modules | 107 passed |
| Default migration/install/database area | 965 passed |
| Seven targets with `AQ_TEST_PG_MODE=pure` | 39 passed |
| Eight actual Bazel rdeps queries and two explanatory somepath queries | Passed |
| Fresh collection and all-module workflow retention | Passed |
| Documentation, generated-command, ownership and remaining manifest checks | 321 passed, 6 opt-in skips |
| Full graph reproduction through repository `.venv/bin/aq` | 1 passed; 159.52s test body |
| Changed-file Ruff and whitespace checks | Passed |

The first graph-reproduction attempt used system Python selected by the PATH
`aq` executable and failed because `pytest-split` was missing. The repository
virtual environment already contains the declared dependency; the focused rerun
uses `.venv/bin/aq` with the same admission controls and passes in 166.28s
total, reproducing all 67 outputs byte for byte. The earlier checkpoint also ran 131
migration/slow cases, 44 substrate cases and 14 PostgreSQL marker cases with two
opt-in skips; these are historical results, not reruns on this final head.

Ownership remeasurement is exactly the dev ledger: 159,208 owned lines,
83,653 engine lines, 62,781 shared lines and 45 tables. This slice has zero
ownership delta and needs no cap increase; existing approvals remain unchanged.

These checks do not claim full Bazel test execution, hermetic execution parity,
a CI cutover, or an execution-time speedup.

## Reproduction

Refresh the catalogue and collect without executing tests, then regenerate:

```bash
python scripts/generate-selection-catalogue.py
python scripts/bazel/inventory.py collect --output /tmp/slice-b-collection.json
python scripts/bazel/generate_targets.py --collection /tmp/slice-b-collection.json
```

The committed manifest retains this slice's 100 opted-in modules. The required
reproducibility test regenerates all 67 outputs and compares their exact bytes:

```bash
aq test tests/test_bazel_manifest.py tests/test_documentation_coverage.py tests/test_generated_command_contracts.py tests/test_integration_size.py
aq test tests/test_install*.py tests/test_migration_*.py tests/test_database*.py tests/test_db*.py
AQ_TEST_PG_MODE=pure aq test tests/test_install_platform.py tests/test_install_node_toolchain.py tests/test_migration_boolean_defaults.py tests/test_migration_json_columns.py tests/test_migration_session_hooks_provisioned.py tests/test_migration_single_head.py tests/test_migration_subagent_events.py
```

Each query has this exact form, with U the sorted focused labels of the stated
universe and S the source label. Use `//src/install:platform.py`,
`//src/install:node_toolchain.py`, `//src/database:migration_guard.py`,
`//src/database:dsn.py`, or `//:migrations/<relative-file>` as appropriate:

```bash
bazel --host_jvm_args=-Xmx2g query --noimplicit_deps --output=label 'rdeps(set(U), S) intersect set(U)'
```

Focused labels are `manifest["modules"][path]["target"]`; PostgreSQL membership
is `manifest["suites"]["postgres"]["modules"]`. The two pure-leaf universes
contain `//tests:test_install_platform` and `//tests:test_install_node_toolchain`.
The exact command vectors, actual labels, independent required sets, and immutable
bindings are retained in the task work logs `.aq/tmp/install-proof-results.json`,
`.aq/tmp/database-query-results.json`, `.aq/tmp/migration-query-results.json`, and
`.aq/tmp/slice-b-parity-results.json`.
