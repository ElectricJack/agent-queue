# Bazel dependency slice D

Task: `swift-pinnacle-16`; PR #2103. This slice migrates the remaining root
test modules, `tests/llm` and `tests/perf`, and repairs the dashboard
build/typecheck graph. Existing workflow arms remain represented; CI is unchanged.
The final graph includes `origin/dev` at `c00df1433`.

## Input discovery

The analyzer follows composed repository paths, literal Python `-c` subprocess
imports, Alembic execution inputs, imported test helpers and fixture dependencies.
Explicit globbing retains matching files; directory iteration conservatively
retains its subtree. Assigning a directory or comparing a path does not read it.
`__file__`-relative fixtures retain each existing enclosing-package interpretation.
Module and class initialization, decorators, defaults and pytest hooks remain
inputs. Unused helper method bodies do not execute during import.

Dynamic helper dispatch keeps a visible conservative fallback. Additive audited
declarations in `scripts/bazel/test_dependency_inputs.json` cover inputs syntax
cannot reveal; unknown imports and unmatched declarations fail generation.
Production modules remain fully scanned.

Discovery snapshots the Git index. Ignored artifacts, untracked files and worker
control files stay outside generator inputs and collection fingerprints. Source
archives provide `.aq/bazel-source-files.json`. Tests retain normal pytest imports
and conftest loading; collection filtering does not provide execution isolation.
The original ignored package artifacts remain present in the worker root.

The slice is the complement of the reserved A integration/train/wait/delivery/
stack/task-graph modules, B migration/install/database/PostgreSQL modules, and C
CLI/API/task/playbook/agent/session/claim modules. The independent audit covers
638 D modules, 24,705 node occurrences, 6,768 direct imports and 12,687 detected
file-read inputs, with no missing imports, reads, markers, labels or memberships.
The exact `c00df1433` baseline has 24,613 D occurrences; all survive.

All sixteen LLM/performance modules retain their conservative fallback, 1,223
source inputs per module and 143 baseline obligations. Knowledge inventory and evaluation resolve without that
fallback. Workspace naming and project paths still reach shared PostgreSQL
fixture/plugin discovery; their conservative result remains explicit.

## Representation and invalidation

Shared filegroups partition labels with identical consumers. Expanding a
module's groups must recover its complete original input set and workflow
metadata. Schema version two interns each distinct ordered dependency list by
the SHA-256 of compact UTF-8 JSON. Strict hydration preserves ordered paths,
duplicates, collection evidence, target labels and workflow arms. Version-one
baselines remain readable through the shared decoder.

The final manifest is 22,242,285 bytes and `tests/BUILD.bazel` is 19,333,947
bytes. All 67 generated files total 42,610,635 bytes, compared with 60,648,254
bytes for those same files on `c00df1433`; every file is below 40 MB.
Independent direct V2 expansion, the shared decoder and a complete re-interning
round trip agree across 1,013 modules, 39,235 nodes and eighteen suites. The
canonical logical SHA-256 is
`9c3192be278564ed3dda31477ff6fbc4d6ebc7e32b9ef2e7634ae8060925d0a4`.

The complete original factoring specimen remains preserved separately. Its
actual Bazel reverse-dependency maps agree across all 8,523 `py_test` targets
and 9,676 reachable source leaves: all 9,677 canonical rows have SHA-256
`e31ec0d5421391c3901d0ee4ac502b41ba425ab39d6f04c7161d92e0b4a5be0e`.
Declared edges decrease from 34,741,145 to 3,316,036; the only 39 added targets
are shared filegroups, with zero unresolved references. All 42 bounded queries
exit zero under the same 2 GB heap budget. Whole-graph XML and graphless
traversal exceeded that budget, so the successful proof extracts complete
dependency frontiers in bounded queries. This is historical factoring evidence,
distinct from the final merged-source checks below.

Dashboard client generation consumes committed `openapi.json`, matching the
legacy npm `--from-file` command. Both dashboard obligations retain frontend
and OpenAPI inputs. Generated exports preserve authored exports; the shared
OpenAPI export permits both frontend and test consumers.

## Generator performance

Profiling the real repository reproduction test located repeated AST parsing,
lexical-index construction, path resolution and large name-history unions.
Each graph now shares parsed trees and caches immutable scan results, import
resolution, reader-relative paths and support templates. Lexical snippet
bindings are built only when a recognized Python subprocess needs them.
Parent and alias indexing share one breadth-first traversal; iterative scanning
preserves depth-first order and `TYPE_CHECKING` pruning. Exact bitsets retain
reference history while preserving existing helper-activation boundaries.
Caches belong to one analyzer/graph invocation, so subsequent generations see
changed source files. Manifest encoding hashes each distinct ordered list once
and reuses the already validated collection instead of decoding it twice.

The actual `test_graph_is_reproducible_and_matches_catalogue` passes in
96.63 seconds for the test body, 98.07 seconds total, and reproduces all 67
outputs byte for byte. Earlier measurements during this repair were 149.98
seconds before the final traversal/history changes and 132.58 seconds under
sampling. The measured result meets the supervisor's approximately 120-second
loaded-machine target; it does not claim the historical 55-second quiet-box
baseline. The test timeout is unchanged.

A separate full comparison ran the reference and optimized implementations in
sequential fresh processes against identical frozen source, index and collection
inputs. Every one of the 67 returned files is byte-identical. Reference generation
took 255.94 seconds; optimized generation took 109.10 seconds. Before/after
bindings agree for both processes, with input-binding SHA-256
`40bc63161d6423b509783c8ef907fcdb4cb7b8b425858de8e1b361f69722c2c7`.
Independent semantic review also checked cache lifetime, lexical resolution,
helper activation boundaries, alias overwrite order and traversal order.

## Final validation

Independent collection contains 39,235 nodes. Against exact merged dev
`c00df1433` (39,143 nodes), zero node identities or markers are removed or
changed, and 92 nodes are added. The older 39,087-node baseline has one
upstream rename: `test_cold_restart_provider_stop_proof_leaves_graph_free`
became `test_cold_restart_external_proofs_leave_transactions_closed` in
`f0a9dca13713`, retaining its graph-lock assertion and adding transaction and
task-row-lock checks. The reproduction timeout marker also changed upstream
in `1fa74a7c58a4`; neither difference is a loss introduced by this slice.

The final focused analyzer, subprocess/path, manifest, CLI, selection catalogue,
documentation and integration-size checks pass 394 tests with six expected
opt-in skips. Ruff, catalogue regeneration and generator-input drift checks pass.
Independent source-only ownership inventories against `c00df1433` are identical:
159,208 owned lines, 83,653 engine lines, 62,781 shared lines and 45 tables.
Every category delta, source-span change and unknown/sink change is zero.

The final scoped proof checks thirteen actual Bazel queries sequentially with
a 2 GB server budget and immutable source/graph bindings. The three provider/
workspace sources each retain exactly 75 focused module targets. The three
LLM/performance probes each retain exactly sixteen targets. The seven frontend
queries check that App.tsx, index.css, tsconfig.json, scenarios/main.tsx and
root openapi.json retain both dashboard build and typecheck obligations, while
facts_parser.py and the unrelated quilt-health-watch fixture retain neither.
These conservative results establish retention and manifest/query agreement;
they do not claim narrow provider or LLM invalidation.

Manifest SHA-256:
`18eebc0876cab10f85d13336410798375ba5142d35125d4bf31c6b4aea259cc0`.
Test BUILD SHA-256:
`2183e00fedb961c1ab502a893bdc076d5efb2046500a4610e4f498817cb26009`.

Full Bazel test execution and two-root frontend artifact execution parity are
separate opt-in acceptance checks and were not run for this slice.

## Reproduction

Stage new source files before collecting. Refresh the catalogue, collect without
executing tests, and retain selected areas through the committed manifest:

```bash
python scripts/generate-selection-catalogue.py
python scripts/bazel/inventory.py collect --output .aq/tmp/slice-d-collection.json
python scripts/bazel/generate_targets.py --collection .aq/tmp/slice-d-collection.json
python scripts/bazel/generate_targets.py --check
python tools/bazel/generator_inputs.py
```

Use `aq test` for focused and area checks and pinned Bazel 8.3.1 for read-only
queries. `scripts/bazel/slice_d_llm_perf.py` audits explicit support/fixture inputs
and retained obligations; its optional `--bazel` flag checks query agreement.
