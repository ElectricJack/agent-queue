# Bazel dependency slice D

Task: `swift-pinnacle-16`. This slice migrates the remaining root test modules,
`tests/llm` and `tests/perf`, and repairs the dashboard build/typecheck graph.
The migration preserves the existing workflow arms; it does not change CI.

## Input corrections

The analyzer recognizes complete composed paths such as
`ROOT / "dashboard" / "src"` and `ROOT / "src" / "database" / "schema.py"`.
Explicit `glob`/`rglob` calls retain matching members; directory iteration retains
the subtree conservatively. A directory assignment does not read its contents.
Bare strings, comparisons and `startswith`/`removeprefix` predicates do not
imply directory reads.
`__file__`-relative fixture paths retain every existing enclosing-package
interpretation. These rules cover the knowledge export JSON, dashboard source
inspection and pane fixtures, and the TypeScript client package manifest.

The separate worktree-ancestor exclusion repair belongs to `swift-meadow-43`.
It was merged from `dev` at `974dbb33b` before regeneration. The disposable
checkout `/tmp/aq-bazel-regen-swift-pinnacle-final` supplies an independent
root outside `.aq` for final regeneration. The earlier
`/tmp/aq-bazel-regen-swift-pinnacle-16` preserves the frozen before/after
factoring specimen.

Literal Python `-c` subprocess snippets contribute their transitive imports.
Unambiguous literal bindings resolve within their lexical scope; conflicting,
mutated or shadowed code stays visibly conservative. Alembic command calls
retain the migration environment and revisions as executable Python inputs.
`scripts/bazel/test_dependency_inputs.json` supplies additive audited `imports`
and `data` glob declarations for inputs syntax cannot reveal. Unknown imports
and unmatched declarations fail generation instead of silently narrowing it.

Imported test support follows referenced functions and classes, including
renamed reexports and aliases. Module initialization, class initialization,
function decorators/defaults and pytest hooks remain inputs. Unused method
bodies do not execute on import. Dynamic helper dispatch keeps a visible
conservative fallback; production modules remain fully scanned.

Dashboard client generation consumes committed `openapi.json`, matching the
legacy npm `--from-file` command. The generated-artifact checks still regenerate
the backend schema. An unused backend fixture dependency is removed from
`dashboard_inputs`. Both dashboard obligations keep frontend and OpenAPI inputs.

## Scope and retained obligations

The slice is the complement of these reserved test-path patterns:

- A: `test_integration_*`, `test_train*`, `test_wait*`, `test_delivery*`,
  `test_stack*`, and `tests/task_graph/`.
- B: `test_migration_*`, `test_install*`, `test_database*`, `test_db*`,
  `test_alembic*`, and the PostgreSQL integration modules.
- C: `test_cli_*`, `test_api_*`, `test_task_*`, `test_playbook_*`,
  `test_agent_*`, `test_session_*`, and `test_claim*`.

`tools/bazel/test_suites.json` records the exact opted-in modules and independent
collection. Proofs compare old node identities and marker obligations, module
target labels, CLI/migration/PostgreSQL arms, and the union of eight default
shards. Adding regression nodes may redistribute default shards; it must not
drop an old obligation.

Most application tests still reach dynamic plugin discovery. Their conservative
closures remain declared. In particular, all sixteen LLM/performance modules
retain that fallback. Source counts alone do not establish narrow invalidation:
query proofs must include files declared as data and the full selected universe.
The final LLM/performance audit retains all sixteen modules and 143 baseline
obligations, with 1,222 source inputs per conservative closure, including the
shared manifest decoder. Three final actual queries for `src/llm/fake.py`,
`src/llm/providers/adapters/gemini_adapter.py` and `tests/llm/fake_genai.py` each select exactly
sixteen of sixteen declared module targets; all exit zero.
The independent 638-module audit found zero missing direct imports or detected
file reads across 12,680 inputs, and retained all baseline node occurrences,
markers, target labels and suite memberships. The slice retains 24,667 node
occurrences, including the 24,581 baseline occurrences. Knowledge inventory and
evaluation resolve without the dynamic fallback. Workspace naming and project paths still activate the shared
PostgreSQL fixture's `LeasePool`, whose migration path reaches plugin discovery.
Their fallback is explicit; the analyzer does not infer the fixture's runtime
early return from environment settings. Three final Bazel queries for
`src/workspace_names.py`, `src/projects/paths.py` and
`src/providers/availability.py` each retain exactly 75 of 75 focused module
targets, matching the manifest with no missing or unexpected targets. These
conservative results establish agreement and retention, without a narrowing claim.

## Repeated input representation

The first complete graph contained 3,971,356 BUILD lines (273,997,842 bytes).
That measurement triggered the supervisor-authorized shared-filegroup factoring.
Each group partitions labels with exactly the same consumers; expanding a
module's groups must reproduce its original inputs and all workflow metadata.

The initial manifest was 282,438,676 bytes. Schema version two stores every
distinct ordered dependency list once in `dependency_sets`, addressed by the
SHA-256 of compact UTF-8 JSON. Each module's four dependency fields reference
that table. Expanding the references reproduces the complete version-one
manifest, including duplicates, collection evidence, targets and workflow arms.
No dependency paths are removed. The installed CLI, generator, tests and audit
script use the same strict decoder; version-one baselines remain readable.

On the first complete candidate, the factored manifest is 22,216,513 bytes and
the BUILD file is 19,324,054 bytes, or 41,540,567 bytes combined. Independent
decoding checks establish exact equality with the unfactored candidate across
1,013 modules, 39,152 node IDs and eighteen suites. The final graph has a 22,614,614-byte manifest and a 19,325,254-byte BUILD
file (41,939,868 bytes combined). All 67 generated files total 42,973,949 bytes,
below the same files on `dev` (62,762,112 bytes); every file is below 40 MB.
Final regeneration binds the equivalence check to the final source and collection
hashes.

Read-only query validation also exposed an overlapping OpenAPI export and a
worktree `.git` control-file input. Generated exports preserve authored exports,
and the shared OpenAPI export permits both frontend and test consumers. Ignored
control files are excluded from literal discovery and the repository-data group.

## Frontend query evidence

All seven read-only queries passed against the final generated workspace,
bound to manifest SHA-256
`c1d1c36b81c97992c75c5c018529e90221e47a4ec43906fb52bec1cb4c19f446`
and test BUILD SHA-256
`52f2593432a33a70ade0e3a51a3d81397aa3a162e1f90d23cb42c9a72217164f`.
After the runtime-control regression update, the manifest hash is
`9316753878517f544245f7a0273a1ce6720f6fe5972e9b01c6e0d09b5005964a`.
Only that regression file's collection source hash changes. All 66 other
generated outputs, module inputs, suite obligations and collection nodes remain
exactly equal, retaining the query graph and its evidence.
For each source, query:

```text
rdeps(deps(set(//dashboard:typecheck //dashboard:build)), SOURCE)
  intersect set(//dashboard:typecheck //dashboard:build)
```

`src/App.tsx`, `src/index.css`, `tsconfig.json`, `scenarios/main.tsx` in the
dashboard package and root `openapi.json` each retain both obligations.
`src/facts_parser.py` and the unrelated quilt-health-watch Python fixture retain
neither. The frontend graph decreases from 19,451 to 11,540 labels, including
1,507 to zero labels from `//src` packages. These are graph measurements;
two-root artifact execution parity is a separate opt-in acceptance check.

## Reproduction

Refresh the catalogue and collect without executing tests, then pass each
selected module as a repeatable `--area` argument. The committed manifest retains
these selections for normal regeneration and drift checks:

```bash
python scripts/generate-selection-catalogue.py
python scripts/bazel/inventory.py collect --output .aq/tmp/slice-d-collection.json
python scripts/bazel/generate_targets.py --collection .aq/tmp/slice-d-collection.json
python scripts/bazel/generate_targets.py --check
python tools/bazel/generator_inputs.py --write
```

Discovery snapshots the Git index, so ignored generated artifacts and untracked
files do not enter analyzer inputs, generated outputs, collection fingerprints,
or test discovery. Source archives use an explicit tracked-path inventory at
`.aq/bazel-source-files.json`. Stage new sources before collecting. The
regression compares full rendered outputs with ignored TypeScript artifacts,
untracked Python/data files and an untracked BUILD boundary present. Runtime
control files remain excluded, and actual worker metadata remains intact.
Pytest's automatic imports and conftest loading retain their normal behavior;
the collection traversal filter is not an execution-isolation layer.
Use AQ admission
for focused and area tests, and pinned Bazel 8.3.1 for read-only queries. `scripts/bazel/slice_d_llm_perf.py` audits explicit support/fixture inputs
and compares retained obligations with a pre-migration manifest. Its optional
`--bazel` flag checks manifest/query agreement without running Bazel tests.

## Verification

Final independent collection retained 39,173 nodes: every one of the 39,087
baseline node IDs and its markers survives, with 86 regression nodes added.
The focused analyzer, subprocess/path, manifest and CLI checks passed 183 tests,
with six expected opt-in skips. Documentation and integration-size checks passed
143 tests. After the runtime-control correction, all 80 analyzer/path checks
passed again. Four additional conflicting subprocess binding probes retained
conservative inputs. The schema-cache source diagnostic retains exactly 143
inputs, including all 136 revisions and `hierarchy_migration.py`.

The integration ownership measurement against `dev` at `974dbb33b` is unchanged:
159,123 owned lines, 83,633 engine lines, 62,732 shared lines and 45 tables.
The delta is zero in every category.

The supervisor authorized publication without waiting for PR #2095. That PR
and slices A–C regenerate on top of this analyzer and renderer after delivery.
The complete actual Bazel reverse-dependency maps match before and after
factoring: 8,523 `py_test` targets and 9,676 reachable repository source leaves.
All 9,677 canonical map rows are identical, with SHA-256
`e31ec0d5421391c3901d0ee4ac502b41ba425ab39d6f04c7161d92e0b4a5be0e`.
Declared edges decrease from 34,741,145 to 3,316,036; the only 39 added targets
are shared filegroups. Both extracted graphs have zero unresolved references.

Whole-graph XML and graphless traversal exceeded the 2 GB query heap budget.
The successful proof extracts actual rules and their complete dependency
frontiers in bounded queries, then computes the full source-leaf/test maps.
All 42 query invocations exit zero within the same budget. The frozen specimen
uses the same explicit root-export repair before and after factoring.

Final full repository generation reproduced all 67 outputs byte for byte
(`test_graph_is_reproducible_and_matches_catalogue`: one pass in 456.22 seconds).
Complete V1/V2 equality covers 1,013 modules, 39,173 nodes and eighteen suites;
the canonical logical SHA-256 is
`99d80d0368a2e0488f3a18162dfe23160218f98c0c23da2b739e53953013c784`.
The complete CLI and selection-catalogue run passed another 114 checks. Ruff
and the catalogue drift check passed after the runtime-control correction.
