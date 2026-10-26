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

The dev foundation manifest records 1,219 application inputs for every knowledge module.
The current source tree has two additional Python files. Inventory's closure
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
Until `swift-meadow-43` fixes path filtering, run these commands in a disposable
checkout whose absolute path has no `.aq` component, as in the repair below.

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

The query counts previously recorded here came from the incomplete graph at
`087e4fa6e` and are invalidated. The analyzer tested absolute path components
against its exclusion list, so generating beneath `.aq` silently omitted
literal directory data inputs. The repaired graph restores these inputs; both
resolved leaf targets include `src/knowledge/budget.py` in `data_files`, even
though it is absent from their Python import closures. The earlier two-leaf
zero-selection result therefore does not describe the repaired graph. No new
Bazel query or isolated-execution result is claimed by this repair.

The 92/88-file import closures remain valid evidence that lazy bootstrap imports
avoid the adapter/plugin fan-out. They do not establish narrow total action
inputs: the repaired inventory/evaluation leaves retain 1,453/1,476 data files.
Further resource-dependency narrowing is separate work.

## Reopened graph repair

Regeneration runs from `/tmp/aq-bazel-regen-fair-glacier-79`, a disposable detached
checkout with no `.aq` path component, using the project Python environment:

```bash
python scripts/generate-selection-catalogue.py
python scripts/bazel/inventory.py collect --output /tmp/fair-glacier-79-collection.json
python scripts/bazel/generate_targets.py --collection /tmp/fair-glacier-79-collection.json
python -m src.cli.app test tests/test_bazel_manifest.py tests/test_selection_catalogue.py -q
```

The repair check passed **96 tests**, with six optional isolated-execution checks
skipped because their opt-in executable settings were absent. In particular,
`test_graph_is_reproducible_and_matches_catalogue` passed from the `/tmp` checkout.
The copied generated files match the validated files byte-for-byte.

Fresh collection contains 39,087 nodes: all 39,073 dev nodes plus the same 14
regressions, with none removed. Regeneration changes only `tests/BUILD.bazel`
and `tools/bazel/test_suites.json` from the previously published head. Compared
with dev, only the two deliberately narrowed leaves remove dependency inputs:
inventory removes 1,128 Python and 2,927 data paths; evaluation removes 1,132
Python and 2,905 data paths. All other opted-in modules retain their baseline
data dependencies. The remaining diff reflects those two closures, the added
helpers/regressions, and collection/workflow metadata; the mass directory-input
deletions are restored.

Supervisor reply `msg-11563375abaa42d8b3d13f3727608442` confirms cached dev
`0d13d9185` is current and the required merge is already up to date. The two
`aq git pull --branch dev` timeouts were daemon load. The supervisor directs
publication without waiting for `swift-meadow-43` or `calm-summit-34`; the five
`test_documentation_coverage.py` failures on dev are inherited, pending the latter
fix. Before publication, dev advanced to `1db2576f1` with the documentation-owner
fix, so that revision is merged as requested. This repair changes no production code and retains the accepted **0 / 0 / 0**
owned/engine/tables disposition.

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
settings were absent; those skips are not execution acceptance. Its graph regeneration and committed-input comparisons passed inside the affected
`.aq` checkout; that historical pass did not detect the missing directory inputs.
The reopened repair above supersedes that graph evidence. Changed-path Ruff and
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
by publication and close. The Python import-closure measurements satisfy that bounded bootstrap proof.
The reopened repair above corrects the separate generated-graph evidence and
withdraws the earlier leaf query claims.
