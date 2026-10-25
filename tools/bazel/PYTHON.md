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
