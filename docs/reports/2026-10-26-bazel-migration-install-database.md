# Migration, installation and database dependency slice

Task: `sharp-flare-92`. Status: blocked checkpoint, as directed by the global
supervisor. Slice D (`swift-pinnacle-16`) and the reopened `fair-glacier-79`
PR #2095 must land on `dev` before final generation and acceptance.

The audited slice covers 100 existing modules: migration, installation,
database/db and the existing PostgreSQL workflow's modules. This checkpoint
publishes runtime seams, explicit pure declarations and their behavior tests.
It excludes generated artifacts and analyzer changes. The coverage and query
results below describe the provisional graph; they require regeneration after
both prerequisites land. This work does not change CI selection.

## Runtime changes

`src.install` defers its existing public exports until requested. Direct adapter
imports avoid loading the installer registry and daemon services. Its 103 public
exports keep their names, order and object identities; star import, `dir()` and
unknown attribute behavior are covered.

The root test fixtures resolve the base DSN through the existing `tests.pg_dsn`
implementation. The `lease_dsn` forwarding helper imports the shared pool only
when called, retaining its current pool and allocation behavior.

Six modules explicitly override PostgreSQL setup because they never connect:
node-toolchain archive verification, boolean-default source scans, JSON-column
source/metadata scans, session-hook source scans, revision-head inspection and
PostgreSQL DDL compilation. Real migration execution retains its database setup.

## Coverage and query evidence

The provisional manifest retains every original 39,087 node ID, without
duplicates, and adds 20 regression cases. All existing workflow obligations
retain their original node identities. Default shards are compared by their
combined coverage because adding tests changes shard assignment.

A real Bazel 8.3.1 query for
`//src/database:queries/project_queries.py`, restricted to the 28 database and
PostgreSQL module targets, returns exactly those 28 targets. This confirms
conservative coverage; it does not demonstrate narrowing.

The installation exclusion proof currently fails: either adapter source selects
all 18 installation modules, including the unrelated pure adapter. Although each
leaf has only 19 imported production files, directory data expansion adds the
whole source tree. In particular, the analyzer interprets
`path.startswith("src/")` in the test-selection catalogue as a directory read.
Its absolute-path exclusion also makes this expansion depend on whether the
checkout's parents contain `.aq`. These analyzer fixes belong to slice D;
the failures are retained for integration, not waived.

Subprocess Python imports, composed repository file reads and Alembic's runtime
revision imports were also audited and handed to the analyzer owner. The final
graph must include those inputs before its soundness proof is accepted.

## Verification so far

All test execution uses AQ admission and its configured worker cap. PostgreSQL
checks use the disposable test service on port 5534.

| Check | Result |
| --- | --- |
| Default migration/install/database area | 965 passed |
| Migration/slow marker arm across all migration modules | 131 passed |
| Database imports, PostgreSQL substrate and DSN helpers | 44 passed |
| Pure-mode installation leaves and five migration modules | 39 passed |
| PostgreSQL workflow marker sample | 14 passed, 2 opt-in skips |
| Focused installer/analyzer regression files | 117 passed, 1 unresolved analyzer regression |
| Provisional graph reproducibility and workflow coverage outside `.aq` | 2 passed |

The unresolved focused regression is
`test_repository_leaf_closure_avoids_database_bootstrap_fanout`: a subprocess
script held in a literal variable is not yet resolved by the analyzer. It is not
a recorded baseline failure. The provisional graph reproduces outside `.aq`;
its remaining broad directory inputs still prevent the narrowing proof.

Final acceptance requires merging current `origin/dev` after the prerequisite
delivery, regenerating outside `.aq` until the path fix lands, passing the graph
reproducibility check, and rerunning actual reverse-dependency proofs. No Bazel
test execution or hermetic-runtime acceptance is claimed by these query results.

## Resume commands

Merge `origin/dev` into the assigned branch after both prerequisites land.
For generation before the location fix is available, use a disposable checkout
outside `.aq` containing that merged source and these checkpoint changes.
Keep AQ's worker database refusal variables unchanged. The disposable test
service used for collection and validation is on port 5534.

```bash
python scripts/bazel/inventory.py collect --output /tmp/slice-b-collection.json
python - <<'PY'
import json
import subprocess
import sys
from pathlib import Path

manifest = json.loads(Path("tools/bazel/test_suites.json").read_text())
areas = ["tests/test_migration_*.py", "tests/test_install*.py",
         "tests/test_database*.py", "tests/test_db*.py"]
areas += sorted(manifest["suites"]["postgres"]["modules"])
command = [sys.executable, "scripts/bazel/generate_targets.py",
           "--collection", "/tmp/slice-b-collection.json"]
for area in areas:
    command += ["--area", area]
subprocess.run(command, check=True)
PY
python scripts/bazel/generate_targets.py --check
aq test tests/test_bazel_manifest.py -k 'graph_is_reproducible_and_matches_catalogue or workflow_obligations_preserve_splits_overlap_and_performance'
```

The exact two-leaf exclusion queries, using the pinned Bazel 8.3.1 executable on
PATH, are:

```bash
bazel --host_jvm_args=-Xmx768m query --noimplicit_deps --output=label 'rdeps(set(//tests:test_install_platform //tests:test_install_node_toolchain), //src/install:platform.py) intersect set(//tests:test_install_platform //tests:test_install_node_toolchain)'
bazel --host_jvm_args=-Xmx768m query --noimplicit_deps --output=label 'rdeps(set(//tests:test_install_platform //tests:test_install_node_toolchain), //src/install:node_toolchain.py) intersect set(//tests:test_install_platform //tests:test_install_node_toolchain)'
```

Require the first to return only `//tests:test_install_platform`, and the second
only `//tests:test_install_node_toolchain`. Then repeat the full 18-installation
and 28-database module-universe comparisons against the final manifest, including
data inputs, and repeat original-node/workflow retention comparisons. Real
application/plugin imports can retain conservative database targets; record that
residual explicitly. A narrow imported-source count alone is insufficient.
