# Python package graph and suite obligations

`scripts/bazel/generate_targets.py` owns the generated `src/**/BUILD.bazel`,
`tests/BUILD.bazel`, `test_suites.json`, and marked sections in the root,
`tools/bazel`, and `checks` BUILD files. The other sections belong to their
respective generators. Refresh after changing tests or fixtures:

```bash
python scripts/generate-selection-catalogue.py
python scripts/bazel/inventory.py collect --output .aq/tmp/bazel-collection.json
python scripts/bazel/generate_targets.py --collection .aq/tmp/bazel-collection.json
python scripts/bazel/generate_targets.py --check
```

Collection runs no tests. Generation refuses failed collection, duplicate IDs,
or stale test/fixture hashes. The manifest retains the independent collection,
catalogue identity, workflow commands, markers, and the installed reference
`pytest-split` version used for the eight duration-balanced default groups.
Node references are indices into one collection table. A module's parametrized
IDs are emitted once in its shared Bazel obligation file.

## Per-area dependency migration

Opt in a selection-catalogue area name or a quoted test-path glob:

```bash
python scripts/bazel/generate_targets.py --area 'tests/test_knowledge_*.py'
python scripts/bazel/generate_targets.py --check
```

Repeat `--area` to select several areas. The manifest retains the opted-in module
names, so normal regeneration and `--check` preserve earlier slices. An unmatched
area is an error. This foundation opts in only `tests/test_knowledge_*`; other
areas retain their existing source declarations until their own migration.
Collection evidence and suite membership are unchanged by opting in. Refresh
collection with the commands above when test or fixture bytes change.

For opted-in modules, `scripts/bazel/test_dependencies.py` follows Python imports,
package initializers, literal dynamic imports, and the transitive fixtures the
module requests. Autouse fixtures, `usefixtures`, fixture overrides and imported
fixtures participate. Conftest startup code and pytest hooks remain dependencies;
unused fixture bodies do not pull unrelated application modules into the graph.
The original conftest bytes remain runfiles. Individual source files are declared
as data, avoiding broad package-library dependencies and import cycles. Unknown
dynamic imports retain a conservative closure and an explanation in the manifest.
Package resources and the existing repository/runtime substrate remain declared;
this does not assert that every input is yet minimal or that a module is pure.

Documentation has a separate `//:python_documentation_data` filegroup and is absent
from `python_repository_data`. Readers retain documentation inputs; unresolved
dynamic consumers may conservatively retain them. Selection proofs should inspect
both the selected modules and their recorded conservative reasons.

The initial knowledge slice does **not** narrow source selection yet. Its root
conftest imports `src.database`; the PostgreSQL adapter reaches `job_queries`,
playbook authority, command inventory, and the plugin registry/loader. Unresolved
plugin discovery therefore retains all Python inputs and documentation. The
supervisor explicitly deferred that bootstrap decoupling to `fair-glacier-79`;
this foundation must not be reported as a measured selection or speed improvement.
The analyzer's synthetic regressions demonstrate narrower declarations when a
module has a resolvable closure, and record why real modules still widen.

Initial validation of `rdeps(//tests:all, //src/knowledge:budget.py)` selected all
8,509 generated test rules, including all 26 opted-in focused knowledge targets.
Each of those modules retained 1,218 application source files. Their recorded
reasons were unresolved dynamic discovery in `src/plugins/internal/__init__.py`
and unresolved file imports in `src/plugins/loader.py`. This is retained evidence
of the bootstrap gap, not a successful narrowing proof.

On the shared host, run execution through `aq test --aq-backend bazel`. It clamps
execution and loading/analysis concurrency to at most two, respecting a lower
admitted worker limit. CPU/memory reservations and one PostgreSQL reservation
bound local test scheduling; pure targets request zero PostgreSQL reservations.
`local_test_jobs=0` keeps those resource reservations active. Direct Bazel defaults
to one execution job and two loading threads. These caps limit this invocation;
they cannot guarantee host load below 16 when other workers are busy. Use query
proofs under load and measure cold execution on a quiet admitted run.

## Legacy areas and retained execution contract

Each immediate `src` package has its own Python library, source/resource
filegroup, and public `generator_inputs`. Static imports include package
initializers. Cross-package import cycles use flattened source filegroups in
`data`, avoiding cycles between libraries. Non-Python package resources are
action inputs. Unresolved dynamic loading, resource discovery, and fixture
lookup widen the dependency closure conservatively. All fixture support files
are included; uncertainty never makes a target pure. The current positive pure
declaration is `tests/test_env_scrub.py`; other modules get private PostgreSQL.
`//tools/bazel:python_generator_inputs` crosses all new package boundaries for
the generated-artifact checks.

Every catalogue module has a focused label such as `//tests:test_env_scrub`.
Workflow variants have separate labels and exact node obligations. The aliases
`//checks:cli`, `default`, `migration_slow`, and `postgres` retain all eleven
Python workflow arms, including repeated obligations. `cli_startup` separately
enables its performance checks; `e2e` retains all four stateful groups.
Variants and opt-in all-node targets are manual. Collection-skipped modules
retain a focused fail-closed target and their original collection reports.

The launcher uses H2's positive environment and private Linux namespaces, H3's
target-owned PostgreSQL lifecycle, and H5's serial pytest plugin and complete
300-second lifecycle budget. It stages declared runfiles, installs project
metadata and console scripts in that private tree, and loads required pytest
plugins explicitly. Each child verifies whole-module collection before selecting
its obligation. The target retains `pytest.json`, `target.json`, and JUnit;
incomplete coverage or missing evidence fails. Neither namespaces nor missing
native ABI inputs fall back to the host runtime.

Native tools retain H1's installation prefix because PostgreSQL embeds its data,
timezone and library paths. The launcher checks that every file in each mounted
native subtree is a declared runfile, rejects escaping symlinks and undeclared
siblings, then mounts those subtrees read-only at their original absolute paths.
The enclosing repository/cache and excluded installation metadata are absent.

Module actions carry Bazel's `no-sandbox` execution tag because the outer Linux
sandbox prevents H2's nested namespace setup. H2 owns the action's user, mount,
network, IPC and PID namespaces and refuses execution if its isolation checks
fail. The private namespace, declared runtime, and positive child environment
remain mandatory on every invocation.

Run the graph checks and focused legacy modules through AQ admission:

```bash
aq test tests/test_bazel_manifest.py tests/test_selection_catalogue.py
aq test tests/test_env_scrub.py tests/test_resolve_repo_path.py
```

Until B4 exposes backend selection, real module execution also uses AQ's outer
test slot. Supply the checksum-verified pinned Bazel executable:

```bash
AQ_BAZEL_EXECUTABLE=/absolute/path/to/bazel-8.3.1 \
  aq test tests/test_bazel_manifest.py -k admitted_bazel_module_acceptance -p no:xdist
```

Unset executable settings visibly skip this opt-in check; a skip is not isolated
backend acceptance. The supervisor-authorized B1 extension supplies H1's pinned
ELF loader/glibc sysroot and H2's restricted read-only ABI mappings. See
`ENVIRONMENT.md` for its refusal and native execution checks.
