# Slice A dependency assessment

Task: `clear-beacon-42`. This slice opts in 122 existing test modules: 83
`test_integration_*` modules, 24 `test_wait*` / `test_delivery*` modules, and
15 modules under `tests/task_graph/`. There are no matching root-level
`test_train*` or `test_stack*` modules. The 26 prior knowledge opt-ins remain.
`tests/perf/test_train_selection.py` remains outside this slice.

The graph package now resolves its authoring exports on demand. Layout imports
retain the model exports without starting graph creation, command discovery or
plugins. An isolated-interpreter regression checks deferred initialization,
public export identities and unknown-attribute behavior.

Source-inspection inputs are explicit beside their consumers. Integration size
inspection declares `src/`, `dashboard/src/` and its ownership manifest; the two
delivery retirement inspections declare `src/`. The wait fault-accounting JSON
is a dashboard input. These are data dependencies rather than Python imports.
The source-inspection audit verifies all 1,661 inventory inputs are covered.

The shared analyzer belongs to `swift-pinnacle-16`, per supervisor guidance
`msg-5d56e28477a34ceb97208da91a930f41`. Generation requires its repository-relative
skip filtering and dashboard path recognition. Absolute filtering incorrectly
ignored directories when the checkout lived beneath `.aq/`. A proposed automatic
bare-directory recognizer was rejected after a real graph check showed it
mistook intermediate `ROOT / "src" / ...` expressions for whole-tree reads.
The explicit consumer declarations avoid that overdeclaration.

Resolved import closures include nine layout modules (93–99 application files),
two integration inspection/documentation modules (88 each), and wait migration
(88). The remaining 110 modules retain real conservative plugin-discovery
closures. Unmigrated legacy areas also retain their package declarations.
These import counts alone are not reverse-dependency or timing evidence.

## Preserved obligations

The collection comparison preserves all 39,087 baseline node identities and adds
only `test_layout_import_defers_graph_services_and_preserves_public_exports`,
giving 39,088 nodes. Slice A contains 6,457 nodes, up from 6,456. All 122 slice
modules are opted in alongside the 26 prior opt-ins (148 total).

The comparison checks each existing node's markers and ordering metadata, each
module's areas, marker expression, PostgreSQL classification and target label,
and each suite's command, marker expression and performance policy. Non-default
suite obligations retain their identities. The eight duration-balanced default
groups redistribute membership after collection changes; their union preserves
every prior default node exactly once and equals the legacy command's selected
node set. CLI obligations remain in their existing separate suite.

These are collection and named-command obligations. They do not establish
source-change selection parity with the installed smart selector's proposal.
That selector combines mandatory ownership/area/critical modules (`M`) with
static impact (`S`). Synthetic single-path observations give 39 mandatory modules
for `src/task_graph/layout/order_key.py` and 103 for `src/integration/models.py`.
Both paths occur in the trusted 1,146-path import-time load closure, causing the
static fallback to propose all 1,012 catalogue modules. A smaller raw Bazel
reverse-dependency set cannot equal that `M union S` fallback proposal.
The observations used synthetic single-modified-path snapshots with the public
static adapter, `pytest-impacted 0.30.0` / `astroid 4.3.2`; they do not describe
the broader working-tree diff. The contracts are `src/test_selection/mandatory.py`,
`static_impact.py` (import-time widening), and `policy.py` (fallback composition).

Smart selection remains shadow-only: those proposals do not instruct this task
to execute the full suite, and ordinary execution still runs the caller's named
targets and marker arm. The actual backend comparison below uses the same four
named order-key test identities. No selection-policy or CI cutover is included.

## Measured precision limit

The v2 generated graph still broadens nominally resolved targets through data.
For example, order-key layout has 93 imported source files but also declares all
1,221 application Python files as data. The analyzer mistakes
`path.startswith("src/")` in `src/test_selection/catalogue.py` for a recursive
source-tree read. Actual Bazel diagnostic queries therefore selected all 15 graph
modules and all 83 integration modules in their respective scoped universes.
These are overdeclaration findings, not successful narrowing proofs.

The shared analyzer owner has the predicate false positive. Removing it must
retain the exact schema hash inputs read by `src.database.schema_key`: tables,
record schema, knowledge protection schema, and hierarchy migration source,
plus migration files. In particular, `hierarchy_migration.py` is read as data
without being imported by layout or wait-migration tests.

The explicit data audit covers all 1,221 source Python files read by each delivery
retirement inspection and the wait fault-accounting dashboard JSON. Final
generation uses a separate checkout outside `.aq` to avoid the known absolute
path-filtering defect. CI and required-check policy retain their existing commands.

## Ownership grant

Supervisor message `msg-bb71c846812341e68b2a81bd464d39ef` approves +314 shared
owned lines, +0 engine lines and +0 tables under
`size-cap-delegation-2026-10-23` (operator decision `6679052c`). The measured
composition is +43 in the lazy facade, plus 132 unchanged parser lines and 139
unchanged validator lines newly reached by the ownership scanner's alias tracing.
The approved baseline 159,123 / 83,633 / 45 becomes 159,437 / 83,633 / 45,
with historical grants preserved and no headroom.

## Validation evidence

The following checks ran through AQ admission with its configured resource caps
and default marker exclusions. The database uses the repository's disposable
test PostgreSQL service, never the operator database.
The table uses the `aq test` spelling; all but the integration-size command were
invoked as `python -m src.cli.app test` from the repository environment.

| Check | Result |
| --- | --- |
| `aq test tests/test_bazel_test_dependencies.py tests/task_graph/ tests/test_task_graph.py -q` | 432 passed; included two temporary analyzer regressions subsequently transferred to the shared analyzer owner |
| `aq test tests/test_wait_faults.py tests/test_wait_migration.py tests/test_delivery_receipt_retirement.py tests/test_delivery_consumers.py tests/test_integration_troubleshooting_doc.py -q` | 180 passed, one skipped |
| `aq test tests/test_integration_size.py` | 136 passed; exact approved caps and historical grants verified |
| `python scripts/bookkeeping_sources.py --check` | Passed |
| Ruff on changed Python sources | Passed |
| `aq test --aq-backend bazel tests/task_graph/layout/test_order_key.py` with the pinned Bazel 8.3.1 executable | Four pytest nodes passed, zero errors/failures/skips; target 81.8 seconds, Bazel elapsed 177.395 seconds |

The mapped Bazel result establishes one real admitted execution. It does not
establish full-suite execution parity, cache-hit parity or a speed improvement.
Its JUnit identities match all four independently collected legacy order-key
node identities, with no missing or extra nodes.
The final `aq test tests/test_bazel_manifest.py tests/test_selection_catalogue.py
-q` run passed 96 checks and skipped six optional acceptance checks in 271.69
seconds. It verifies the graph generated outside `.aq` against the final 39,088-node
collection. An earlier run found a stale generator-input inventory after the
grant measurement file was added; refreshing the inventory resolved it.

Positive Bazel queries on the v2 graph returned nine of nine required order-key
source/fixture/schema labels and a direct target-to-order-key-source path. A timer
reverse-dependency query over wait-listener and wait-migration returned both
expected targets. Full dependency queries confirmed the wait fault-accounting
dashboard JSON and every one of the 1,221 Python files inspected by delivery
retirement. These prove input retention while the source-predicate
overdeclaration remains a separate precision limit.

For example, the following query returned both named wait targets:

```sh
bazel query 'kind("py_test rule", rdeps(set(//tests:test_wait_migration //tests:test_wait_listener), //src/waits:adapters/timer.py))' --output=label --noshow_progress
```

The source-path query returned the target and its direct source dependency:

```sh
bazel query 'somepath(//tests:task_graph__layout__test_order_key, //src/task_graph:layout/order_key.py)' --output=label --noshow_progress
```

The query executable was `/tmp/aq-b2-bazel-8.3.1`, SHA-256
`17247e8a84245f59d3bc633d0cfe0a840992a7760a11af1a30012d03da31604c`.
Queries do not execute tests. Interrupted full-universe queries supply no usable
selection count and are excluded from the evidence.
