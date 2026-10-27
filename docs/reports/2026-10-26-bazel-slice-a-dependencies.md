# Slice A dependency assessment

Task: `clear-beacon-42`. Current base: dev `266ddce1f`. This submission is blocked
awaiting PR #2103's V2 manifest factoring. Supervisor decision
`msg-3b1c8fb5793b4c968f999329bbfee4c4` authorizes publishing owned source, the
approved ownership reconciliation, and this report only. Regenerated Bazel
outputs remain local evidence; no shared analyzer or formatter change is included.

The slice targets 122 existing modules: 83 `test_integration_*`, 24 root
`test_wait*` / `test_delivery*`, and 15 under `tests/task_graph/`. There are no
matching root `test_train*` or `test_stack*` modules. The performance-only train
selection module remains outside this slice. CI and smart-selection policy
remain unchanged.

The graph facade defers authoring services until requested while preserving its
public exports. Its isolated-interpreter regression exercises deferred imports,
export identity, and unknown attributes. Explicit source-tree inspection inputs
remain beside integration-size and delivery-retirement consumers.

## Current acceptance and handoff

Acceptance requires Bazel reverse dependencies to contain legacy's real
mandatory set `M`, together with collection and named-command parity. The
shadow-only static fallback `S` is not a precision target.

Independent synthetic single-modified-file observations use the public
`mandatory_set` API and a complete observation, without static fallback:

| Changed source | Mandatory modules across the catalogue | Mandatory modules in the audited group |
| --- | ---: | ---: |
| `src/task_graph/layout/order_key.py` | 39 | All 15 graph modules |
| `src/integration/models.py` | 103 | 65 of 83 integration modules |

Both graph-layout and task-graph-creation ownership rules apply to order-key.
A proposed nine-module graph subset would omit six mandatory modules under the
current rules: active-expansion, cost, flow, layering, ordering, and view. Final
precision work must retain `M`; import-closure precision alone is insufficient.

Slice D is not an ancestor of this base. The current analyzer still treats
`startswith("src/")` as a source-tree data read, so raw reverse dependencies
remain overdeclared. No successful narrowing or source-selection cutover is
claimed. The shared-hook handoff must:

1. Merge settled dev after #2103 and use its additive JSON declaration hook.
2. Preserve the delivery source scans, integration-size source/dashboard scans,
   the wait fault-accounting dashboard JSON, imported test helpers, and exact
   schema-hash inputs, including hierarchy migration and every migration file.
3. Regenerate, compare named-command node identities, and re-run source-change
   proofs against `M` with the same generated graph. Keep conservative paths
   unless their actual dependencies are proven.

The current V1 generation produces `tests/BUILD.bazel` at 89,781,545 bytes and
`tools/bazel/test_suites.json` at 110,245,748 bytes. The latter exceeds GitHub's
100 MiB limit; both exceed the approved 40 MB generated-file budget. The source-only
publication deliberately retains dev's generated graph. After #2103 lands, the
supervisor will reopen this task for V2 regeneration and final proofs.

Independent collection contains 39,110 nodes: all 39,109 dev nodes plus the new
facade regression. The diagnostic generation opts in 122 slice modules, increasing
fine-grained modules from 26 to 148. Comparison found no missing identities and
preserved commands, markers, PostgreSQL requirements, ordering and non-default
obligations. These unpublished artifacts establish collection parity, not a
completed dependency rollout.

Diagnostic Bazel queries selected all 83 integration targets for `models.py`,
retaining all 65 mandatory targets with 18 extras. Both delivery scans retain all
1,221 source Python files; the containment spec, 47 wait source files, schema
inputs, migrations and fixture helpers are retained. The wait-fault dashboard JSON
is missing from the generated dependency graph and remains a required correction
through slice D's shared hook. Timer reverse dependencies select all 24 wait and
delivery targets. Three graph queries timed out during cold external-repository
preparation; a fourth confirmed the unwanted direct dependency on `creator.py`.
Graph reverse-dependency and retention proofs remain incomplete. No current Bazel
build or execution-parity claim is made.

Detailed evidence is under
`.aq/tmp/slice-a-resume/`, `.aq/tmp/slice-a-resume-graph/`,
`.aq/tmp/slice-a-resume-wait/`, and `.aq/tmp/slice-a-resume-integration/`.

The heartbeat still reports `stack_stale_input` after the current dev merge; the
supervisor identified this as prerequisite-identity bug `steady-vault-53`.

## Ownership reconciliation

The previously approved +314 shared owned lines, +0 engine lines and +0 tables
are applied once to dev's 159,166 / 83,653 / 45 ledger, yielding exactly
159,480 / 83,653 / 45 with no headroom. The composition remains +43 facade
lines plus 132 unchanged parser lines and 139 unchanged validator lines newly
reached through aliases. Original decision `6679052c`, grant history and the
original measurement remain intact. A separate merge remeasurement references
the original composition and explicitly creates no new grant.

The live inventory has no budget errors. `scripts/bookkeeping_sources.py --check`
passes, and `aq test tests/test_integration_size.py -q` passes 136 tests.

## Current verification

The isolated graph-command check passes eight tests, and documentation coverage
passes seven. Ruff on changed Python files and `git diff --check` pass.
The area check across `tests/task_graph/`, `test_task_graph.py`, wait faults and
migration, delivery retirement and consumers, and the troubleshooting document
passes 592 tests with one skip. These checks use the separate disposable
PostgreSQL service; operator database settings remain unchanged.

`aq test tests/test_bazel_manifest.py tests/test_selection_catalogue.py -q`
finishes with 100 passed, six skipped and one failure: the reproducibility test's
`/usr/bin/python3.12` interpreter cannot import `pytest_split`. This is a test
environment limitation, not a successful reproducibility check. The unpublished
graph was generated separately; its collection comparison is recorded above.
Final generated-file reproducibility and inventory checks remain due after V2
regeneration, including the new ownership-measurement input shards.

## Prior checkpoint evidence

Before recovery, the previous worker recorded focused checks (432 passed),
consumer checks (180 passed, one skipped), and manifest/catalogue checks
(96 passed, six optional skips). Its admitted Bazel order-key execution passed
four nodes with exact independent legacy collection identity parity. These are
historical results; the current submission's checks are recorded separately.
They do not prove full-suite execution parity, cache-hit parity, or a speedup.
