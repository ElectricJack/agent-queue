# Slice A dependency assessment

Task: `clear-beacon-42`. Settled base: dev `512996c47`. This submission applies
slice D's shared V2 generator and additive declaration hook to 122 existing
modules: 83 integration, 24 wait/delivery and 15 task-graph modules. No root
`test_train*` or `test_stack*` module currently exists; the audit includes those
prefixes for future modules. CI, legacy commands and selection policy are unchanged.

## Acceptance and result

The supervisor's recorded acceptance is Bazel dependency containment of legacy's
real mandatory set `M`, plus collection and named-command parity. Shadow-only
fallback `S` is not a precision target. The reusable audit is
`scripts/bazel/slice_a_dependencies.py`; it evaluates complete synthetic
single-modified-file observations through the public `mandatory_set` API.

The audit checks all 1,223 tracked source Python paths against the 122-module
slice. Additive declarations preserve every mandatory edge even where import
analysis alone would select fewer tests. They also retain runtime reads: source
inspection scans, ownership dashboard scans, the wait-fault dashboard JSON,
trust manifests, historical replay fixtures and containment documentation.

All 122 modules currently retain a conservative full-source closure. The shared
analyzer reports unresolved dynamic imports in
`src/plugins/internal/__init__.py` and an unresolved file import in
`src/plugins/loader.py`. Integration-size additionally reaches escaped helper
code. **This submission does not demonstrate narrower source-change selection.**
It preserves mandatory coverage and documents the shared precision limitation;
no local analyzer overlay is included.

Pinned Bazel 8.3.1 queries agree exactly with the manifest and omit no mandatory
module. Counts below are within the indicated slice group, not across the whole
repository:

| Modified source | Group | Mandatory M | Bazel selected |
| --- | --- | ---: | ---: |
| `src/task_graph/layout/order_key.py` | graph | 15 | 15 |
| `src/task_graph/creator.py` | graph | 1 | 15 |
| `src/task_graph/parser.py` | graph | 0 | 15 |
| `src/integration/models.py` | integration | 65 | 83 |
| `src/waits/adapters/timer.py` | wait/delivery | 19 | 24 |

The common audit exits zero for all 1,223 source checks and all five queries.
The slice contains 6,474 independently collected nodes with identical markers
and order. The settled manifest SHA-256 is
`86b9364e919ebeee1c0829f7c75ee80cda9f15cff3e67157cd6d5df1a89f09af`;
the tests BUILD SHA-256 is
`a6d1f543d72eb688684781a50272ebd558671ee39ff73a085f28e86b44a5693c`.

Independent group audits also retain 11,166 integration mandatory edges, nine
integration runtime-reader sets and 143 schema inputs. Additional integration
probes select 83 modules for candidate-baseline M=50 and trust-manifest M=56.
Four wait probes each select 24 for M=19. Wait/delivery closure checks preserve
both complete source scans, 47 wait sources, dashboard JSON, containment spec,
142 schema inputs per module and 51 direct helper-import obligations. No runtime
input omission was found. All dedicated query servers were shut down afterwards.

The graph facade defers authoring services until requested while preserving all
20 public exports. Its isolated-interpreter regression covers deferred imports,
export identity, caching and unknown attributes. This runtime improvement does
not overcome the analyzer's conservative fixture/plugin closure.

## Collection and workflow parity

Independent collection exits zero with 39,236 nodes: every one of dev's 39,235
identities and marker sets, plus the facade regression. All 1,013 modules retain
prior node obligations and non-default workflow memberships. Existing named
commands, marker expressions, PostgreSQL requirements and collection ordering
are retained. The eight timing-balanced default splits redistribute after the
new node; their union retains every old obligation. This changes 364 empty/filled
module-split target memberships out and 358 in, with zero non-default target
removals. It is not a loss of tests or a CI change.

Fine-grained opt-ins increase from 638 to 760. The V2 manifest and generated
BUILD files replace the previously unpublished oversized V1 artifacts.
`tools/bazel/test_suites.json` is 27,107,459 bytes, `tests/BUILD.bazel` is
17,098,536 bytes and `checks/BUILD.bazel` is 353,504 bytes. Large generated-line
deletions reflect shared-input factoring; the audit verifies retained obligations
across all 1,013 modules.

## Ownership reconciliation

The existing approved +314 shared owned lines, +0 engine lines and +0 tables
are applied once to dev's 159,208 / 83,653 / 45 ledger, yielding exactly
159,522 / 83,653 / 45 with zero headroom. The composition remains +43 facade
lines plus 132 unchanged parser lines and 139 unchanged validator lines newly
reached through aliases. Original decision `6679052c`, every historical grant
and earlier measurement remain intact. The separate
`bazel_slice_a_v2_merge_measurement` records this composition on current dev;
it creates no new grant.

## Verification

The focused graph-command, integration-size, documentation-coverage and generated
command-contract check passes 276 tests. The area check covering task graph,
wait faults/migration, delivery consumers/retirement and integration documentation
passes 592 tests with one skip. Tests use the project virtualenv to run
the same AQ wrapper (`python "$(command -v aq)" test ...`), retaining box admission
and worker caps. The installed `/usr/bin/python3.12` AQ interpreter lacks
`pytest_split`; no package installation or shared environment change was needed.
The disposable PostgreSQL test service is `:5534`; operator settings remain intact.

The final Bazel-manifest, selection-catalogue, documentation-coverage and
generated-command-contract check passes 242 tests with six skips, including
generator reproducibility (173.69 seconds), catalogue regeneration, new-file
ownership and locked generator inputs. Ruff, bookkeeping projection drift,
generator-input drift and Git whitespace checks pass. No tests were weakened.

Current evidence is under `.aq/tmp/slice-a-final-*`, including the independent
collection, retained dev manifest, M audit, query logs and test outputs. No full
suite, Bazel execution/cache parity or performance speedup is claimed.

## Reproduction

Stage authored inputs before generation; source discovery snapshots the Git index.
Retain the dev manifest independently before regenerating. Collect without running
tests, then generate and audit:

```bash
git show 512996c47:tools/bazel/test_suites.json > .aq/tmp/slice-a-dev-manifest.json
python scripts/bazel/inventory.py collect --output .aq/tmp/slice-a-final-collection.json
python scripts/bazel/generate_targets.py --collection .aq/tmp/slice-a-final-collection.json \
  --area 'tests/test_integration_*' --area 'tests/test_wait*' \
  --area 'tests/test_delivery*' --area 'tests/task_graph/*'
python tools/bazel/generator_inputs.py --write
python scripts/bazel/slice_a_dependencies.py \
  --baseline .aq/tmp/slice-a-dev-manifest.json \
  --collection .aq/tmp/slice-a-final-collection.json \
  --bazel /path/to/pinned/bazel-8.3.1 \
  --output-base /tmp/aq-slice-a-audit-bazel --output .aq/tmp/slice-a-final-audit
```

The audit performs read-only queries with a 2 GiB JVM limit. It records source,
catalogue, policy, manifest and BUILD digests; asserts mandatory-set containment,
direct imports and parity; and reports actual selection breadth. Required test
execution continues through `aq test`.
