# Bazel dependency slice C

Task `wise-horizon-21`, resumed claim epoch 2. The final implementation uses
slice D's shared analyzer and factored V2 generator from dev
`512996c4738a348ac2a6372697f184e20a955a8c`. No local analyzer overlay remains.
The slice contains 170 modules: 39 CLI, 17 API, 37 task, 36 playbook, 23 agent,
17 session and one claim module. Existing opt-ins remain enabled. CI and the
legacy runner remain unchanged.

## Changes and audited inputs

`scripts/bazel/test_dependency_inputs.json` adds four additive declarations:

- CLI module entry imports `src.cli.app` through subprocess/embedded Python
  and enumerates `src/cli/*.py`.
- CLI module map reads `src/cli/AGENTS.md` and enumerates `src/cli/*.py`.
- Playbook collections consumes the dashboard `collections.fixture.json`.
- Playbook API DTOs consumes the dashboard `graph.fixture.json` through a helper.

The declaration file is mapped to the existing Bazel and test-selection owners
in `tests/selection_rules.yaml` so legacy shadow selection covers its changes.

All 170 modules were audited in three parallel groups. No further missing
inputs were found. Profile/harness directories retain their package resources;
repository scans and executable migrations retain their real inputs. The
session-launch AST ratchet intentionally reads every production Python file.
Unknown dynamic imports retain their conservative explanations.

Two pure modules now use the established test-local no-op `_pg_backend`
fixture: `test_cli_menus` (ten mocked-prompt cases) and `test_task_context`
(four dataclass-field cases). Neither uses a database, directly or through
helpers. All fourteen tests, assertions and markers remain. Root hooks and
other autouse fixtures still run. The supervisor approved this exact approach
in message `msg-26047bf082b042a7b7a7f10f92251426`.

The CLI leaf resolves to 21 application Python files and the context leaf to
18, each with five test/support files and no conservative fallback. The other
168 modules remain conservative. Their shared PostgreSQL fixture reaches
`tests/db_fixtures.py::_migrate`, database schema setup, executable Alembic
inputs, `migrations/env.py`, configuration and plugin loaders. Slice D's
improved Alembic discovery correctly exposes that dependency. The earlier
checkpoint's eight resolved leaves and pre-fix query counts are superseded.

## Meaning of the selection proof

Let M be the tests whose actual inputs or behavior depend on a changed file.
The query proof retains direct consumers and every unresolved conservative
consumer. It excludes only the two positively audited pure leaves when they
have no dependency on that file. A conservative target is a required safe
selection, not a claim that each test case executes every retained source.

The proof uses checksum-verified Bazel 8.3.1
(`17247e8a84245f59d3bc633d0cfe0a840992a7760a11af1a30012d03da31604c`),
one shared server with a 1 GB heap and two loading threads, and no Bazel test
execution. The full universe includes every suite variant; canonical C-module
selection must equal the manifest's declared input membership, and every
variant must agree with its canonical module. Representative invocation:

```bash
/tmp/aq-b2-bazel-8.3.1 \
  --output_user_root=/tmp/aq-slice-c-epoch2-bazel \
  --host_jvm_args=-Xmx1024m query --loading_phase_threads=2 \
  'kind("py_test rule", rdeps(//tests:all, //src/cli:menus.py))'
```

The positive/negative probes are `src/cli/menus.py`,
`src/task_graph/activity.py`, `src/models.py`, and the two dashboard fixtures.
Menus must select the menu leaf and exclude the context leaf; activity must
exclude both pure leaves; models must retain the context leaf. Broad genuine
or unresolved dependencies remain selected. This is conservative selection
with two verified exclusions, not global minimal invalidation.

All five full-universe queries exited zero. Their exact selected counts are:

| Changed input | C modules / 170 | All-universe test variants |
| --- | ---: | ---: |
| `src/cli/menus.py` | 169 | 8,490 |
| `src/task_graph/activity.py` | 168 | 8,481 |
| `src/models.py` | 170 | 8,516 |
| Dashboard collections fixture | 168 | 6,938 |
| Dashboard graph fixture | 168 | 6,938 |

Every queried variant agrees with its canonical module. The independent CLI/API
query repeats the menus/activity probes and selects 56/56 and 55/56 modules,
respectively, excluding only the pure menus module for activity.

## Preserved obligations and representation

Independent collection contains 39,235 nodes, exactly matching dev by identity
and markers. The 1,013-module manifest retains every original obligation:
CLI 448, PostgreSQL 76, migration/slow 215, startup one, four E2E arms 5/6/6/5,
module 38,831, default-shard union 38,383, and all-node 39,235. The eight default
shards are compared as a multiset union. Commands, markers, performance flags,
workflow identity and every nondefault per-module arm remain unchanged.
No assertion, test, suite or execution arm is removed.

The independent audit expands V2 dependency lists and every shared BUILD
filegroup. It checks all module input memberships and all generated variant
node/marker/performance obligations. Outside-C inputs may grow additively
because repository scanners see the declarations and this report; none are
removed. Large textual BUILD deletions reflect shared input factoring rather
than missing directory inputs. Dev's BUILD contains 19,333,947 bytes and
336,766 lines; the final measurements and query receipts are recorded below.

The final BUILD is 16,554,088 bytes / 255,815 lines, SHA-256
`b674654a148c335b2f6b3a5256da94b51f2a70353226313e521d5b37e77da1f6`.
The final manifest is 23,140,977 bytes / 279,776 lines, SHA-256
`4d9fcd134e688a424785099f319ad00a4de6238903b0b125f7a0e055ca827312`.
Forty-three shared BUILD input groups expand to 4,906,385 module/data
memberships; the manifest stores 4,990,269 logical entries in 161 unique
input sets. The independent audit passed 242,793 assertions, including
11,143 module/arm comparisons. Fresh collection SHA-256 is
`a59be2f1de534efed9e3753f8c1793674f47e0f4f4a54b5be96171ab4e066762`.

Production source ownership remains 159,208 total lines, 83,653 engine lines
and 45 tables, with every category/span/sink unchanged from dev. There is no
production change or size grant to compose. The supervisor explicitly accepted
this zero delta on the current ledger.

## Reproduction and validation

```bash
python scripts/generate-selection-catalogue.py
python scripts/bazel/inventory.py collect --output .aq/tmp/slice-c-final-collection.json
python scripts/bazel/generate_targets.py \
  --collection .aq/tmp/slice-c-final-collection.json \
  --area 'tests/test_cli_*.py' --area 'tests/test_api_*.py' \
  --area 'tests/test_task_*.py' --area 'tests/test_playbook_*.py' \
  --area 'tests/test_agent_*.py' --area 'tests/test_session_*.py' \
  --area 'tests/test_claim*.py'
```

Collection executes no tests. The committed manifest retains the opt-ins, so
later generation needs no `--area` arguments. The reproduction test checks
all generated files byte-for-byte.

The admitted broad check covered `test_bazel_manifest`, documentation coverage,
generated command contracts, analyzer/subprocess dependencies, Bazel CLI and
integration size. It passed 463 tests and skipped six opt-in execution checks;
its sole failure was graph reproduction because the installed `aq` launches
`/usr/bin/python3.12`, which lacks `pytest-split`. The repository Python has
the pinned `pytest-split 0.11.0`; final reproduction uses its AQ entry point,
`python -m src.cli.app test`, with the same box admission and worker caps.
The two changed modules independently passed 10 and 4 cases. Ruff and whitespace
checks pass. No full-suite execution, hermetic execution parity, cache-hit
acceptance or performance speedup is claimed.

The repository-interpreter rerun passed 123 tests, including byte-for-byte
graph reproduction, with six opt-in skips. Its single selection-owner failure
identified the declaration JSON mapping added above; all 56 selection-suite
tests passed after that fix. Exact admitted commands (with the test PostgreSQL DSN
configured) are:

```bash
aq test tests/test_bazel_manifest.py tests/test_documentation_coverage.py \
  tests/test_generated_command_contracts.py tests/test_bazel_test_dependencies.py \
  tests/test_bazel_subprocess_dependencies.py tests/test_bazel_cli.py \
  tests/test_integration_size.py -q
python -m src.cli.app test tests/test_bazel_manifest.py \
  tests/test_selection_catalogue.py tests/test_cli_menus.py tests/test_task_context.py -q
python -m src.cli.app test tests/test_selection_catalogue.py -q
python tools/bazel/generator_inputs.py
ruff check tests/test_cli_menus.py tests/test_task_context.py
git diff --check
```

Local detailed receipts are `.aq/tmp/slice-c-parity-final.json`,
`.aq/tmp/slice-c-query/proof.json`, `.aq/tmp/slice-c-cli-api-query-evidence.json`,
`.aq/tmp/slice-c-size-proof.json`, and the `slice-c-*-validation.log` /
`slice-c-selection-final.log` logs. The committed report records their
measurements and commands; the workspace receipts are not required inputs.
