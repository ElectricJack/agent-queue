# Database bootstrap dependency assessment

Task: `fair-glacier-79`. Prerequisite: `stark-orbit-64`, landed on dev at
`0d13d9185eefffde0d9541e7c4cd2165d28c071d`, merged into this branch at
`88aa4ccbf`. This assessment uses the real prerequisite analyzer and generator,
not the earlier provisional source overlay.

## Result and limits

Lazy database/package query exports, fixture-local configuration and adapter
imports, and a standalone DSN-redaction helper remove the shared
conftest-to-adapter-to-plugin dependency chain. Public production exports keep
their identities, and reusable adapters retain their initialization and cleanup
behavior. The internal test helper moves from `tests.db_fixtures` to
`tests.reuse_databases`; its callers move with it.

| Opted-in module | Application files | Conservative fallback |
| --- | ---: | --- |
| `tests/test_knowledge_inventory.py` | 92 | No |
| `tests/test_knowledge_evaluation.py` | 88 | No |
| Remaining 24 knowledge modules | 1,221 each | Yes |

The foundation recorded 1,218 application inputs for every knowledge module.
The current source tree has three additional Python files. Inventory's closure
also includes five test/support files and four other Python files. The regression
`test_repository_leaf_closure_avoids_database_bootstrap_fanout` verifies its real
source and conftest dependencies and excludes the PostgreSQL adapter, job queries
and plugins. It deliberately avoids freezing incidental file counts.

This is a bootstrap improvement, not a global test-selection or speed claim.
For example, the budget test's own `src.config` import leads through playbooks,
script inputs, git/integration and the orchestrator to the application graph.
The unresolved dynamic discovery in `src/plugins/internal/__init__.py` and file
import in `src/plugins/loader.py` then retain all Python inputs. Legacy modules
outside the opted-in slice also retain broad package declarations. Removing those
independent dependencies or migrating every legacy area is outside this refactor.

## Reproduce

Use the repository's disposable test PostgreSQL DSN for collection and pytest.
Collection executes no tests; it preserves all marker arms and collected nodes.

```bash
python scripts/generate-selection-catalogue.py
python scripts/bazel/inventory.py collect --output .aq/tmp/bootstrap-collection.json
python scripts/bazel/generate_targets.py --collection .aq/tmp/bootstrap-collection.json
python scripts/bazel/generate_targets.py --check
```

The committed manifest retains the original 26 opted-in knowledge modules. Its
`modules` entries record source files and conservative reasons. Query with the
checksum-verified Bazel 8.3.1 executable from `tools/bazel/toolchains.lock.json`:

```bash
bazel query 'kind("py_test rule", rdeps(//tests:all, //src/knowledge:budget.py))'
bazel query 'kind("py_test rule", rdeps(set(//tests:test_knowledge_inventory //tests:test_knowledge_evaluation), //src/knowledge:imports/inventory.py))'
bazel query 'kind("py_test rule", rdeps(set(//tests:test_knowledge_inventory //tests:test_knowledge_evaluation), //:scripts/evaluate-knowledge.py))'
```

The first query keeps the full universe visible, including conservative and
legacy targets. The other queries compare the two resolved leaves; they are not
substitutes for the first query.

Measured `py_test` rule counts after regeneration:

| Query | Selected rules |
| --- | ---: |
| All `//tests:all` | 8,531 |
| Full-universe reverse dependencies of `budget.py` | 8,511 |
| Full-universe reverse dependencies of `imports/inventory.py` | 8,521 |
| Two-leaf universe, `imports/inventory.py` | 1: `test_knowledge_inventory` |
| Two-leaf universe, `scripts/evaluate-knowledge.py` | 2: both leaves |
| Two-leaf universe, `budget.py` | 0 |

Among the 26 focused knowledge targets, the budget query retains 24, excluding
inventory and evaluation. Both leaf tests actually use the evaluation script,
so retaining both for that input is expected. The full-universe result remains
broad: the 20 rules absent from the budget result are the focused/workflow
variants of the two resolved leaves. This is not global precise selection.

## Validation and ownership

The post-merge legacy-runner checks use AQ's normal admission and marker policy:

```bash
aq test tests/test_bazel_test_dependencies.py tests/test_database_imports.py tests/test_database_backend_selection.py tests/test_database_engine.py tests/test_database_postgresql.py tests/test_database_channels.py tests/test_migration_guard.py -q
aq test tests/test_database.py tests/test_knowledge_inventory.py -q
aq test tests/test_postgres_test_substrate.py -m slow -k 'initialized_database_survives or reuse_database_keeps' -p no:xdist -q
python -m src.cli.app test tests/test_bazel_manifest.py tests/test_selection_catalogue.py -q
```

The four commands passed 99, 125, 2 and 96 tests respectively. The last command's
six optional isolated-execution checks skipped because their opt-in executable
settings were absent; those skips are not execution acceptance. Its full graph
regeneration and committed-input comparisons passed. Changed-path Ruff and
whitespace checks passed. Fresh independent collection found 39,087
nodes: all 39,073 prerequisite nodes plus 14 new regression nodes, with none
removed. The project-environment CLI invocation uses the same AQ admission gate;
the system-Python `aq` entrypoint lacks the required `pytest-split` dependency.
The generated input inventory was refreshed with
`python tools/bazel/generator_inputs.py --write`. Ignored local dashboard build
outputs were kept outside the source tree during generation.

No full-suite or isolated Bazel execution pass is claimed: worker policy
requires focused/area checks and leaves the full suite to CI.

Canonical `tests.integration_ownership.inventory` measurements on a read-only
archive of dev `0d13d9185` and the final source tree both report 159,123 owned
lines, 83,633 engine lines and 45 tables. Task delta: **0 / 0 / 0**. The supervisor's
earlier zero-delta/no-grant disposition therefore still applies; the updated
measurement was sent as `msg-aef7dd20bcb2458e9718234010d2c921`.
Supervisor reply `msg-777ad73eb25c42ad9c85881e03fd380e` approved the remaining
helper split and requested the real leaf no-whole-application proof, followed
by publication and close. The measurements above satisfy that bounded proof;
they do not claim removal of the independent conservative closures.
