---
name: aq-bazel-upgrade
description: Add reproducible Bazel test support to an existing AQ-managed Python or JS/TS repository, with generated targets, deterministic shards, parity evidence and AQ runner/train integration. Use for a requested Bazel upgrade; excludes CI cutover and AQ feature development.
allowed-tools:
  - Bash
---

# Upgrade a managed project to Bazel tests

Build an additive, reproducible test graph that AQ can consume. Keep the project's
existing pytest/npm commands working as the fallback. Complete the repository
work and report integration readiness; do not switch CI, required checks,
attestations, branch protections or installed train policy in this workflow.

## 1. Establish the contract before BUILD authoring

Read the repository's agent instructions, accepted testing strategy, dependency
files, fixtures and current test/CI commands. Inventory each obligation: collected
Python node IDs and marker expressions; JS/TS tests, typechecks and builds;
generators; integration/E2E scenarios; runtime tools, data and services. Preserve
exclusions and intentional overlaps between suites. Record counts, timings
including setup/teardown, known failures and flaky tests. Resolve an absent or
unclear testing strategy with the owner before choosing coverage; adding Bazel
does not authorize removing tests or moving checks after merge/deployment.

Inspect `aq test --aq-help` and the installed AQ backend/manifest validator and
train signature consumer. Confirm how they select a project root and manifest.
Do not assume that a flag supported by another AQ checkout exists here, that
the runner accepts arbitrary manifest JSON, or that AQ's own repository paths
work for another project. Record the actual accepted schema/version and mapping.
If a required consumer is absent or hard-coded to AQ, finish the portable graph,
retain legacy execution and report the missing integration separately. Do not
implement a new AQ feature or fabricate a config key to make this skill pass.

Provenance for maintainers with an AQ checkout/vault: this follows H
(`bold-rapids-18`), B (`amber-summit-41`), V (`crisp-cascade-23`) and T
(`bright-bridge-75`) in
`projects/agent-queue/specs/2026-10-23-bazel-for-aq-hermetic-tests-parity-measured-cutover-and-trai.md`.
H's reference files are `MODULE.bazel`, `tools/bazel/README.md`,
`ENVIRONMENT.md` and `runtime-contract.md`; when absent from dev, inspect with
`git show origin/aq/epic/bazel-h-pinned-toolchains-and-hermetic-test-substrate:MODULE.bazel`
and the corresponding paths. B's contract includes `generate_targets.py`,
`pytest.bzl`, `pytest_main.py` and `test_suites.json`; V adds `impact.py` and
`compare_results.py`; T's signature contract is in `src/integration/signatures.py`.
These are reference contracts, not proof that every component is installed.

## 2. Pin resolution and execution tools

Commit `.bazelversion`, `MODULE.bazel`, generated `MODULE.bazel.lock`, `.bazelrc`
and a small `tools/bazel/` toolchain/dependency lock area. Start with the languages
the project uses. AQ's tested reference pins are Bazel 8.3.1, rules_python 1.8.2,
aspect_rules_js 3.5.1 and aspect_rules_ts 3.10.1; verify compatibility for the
project's platform rather than automatically selecting newest releases.

For a combined Python/JS/TS graph, the reference module dependencies are:

```starlark
bazel_dep(name = "rules_python", version = "1.8.2")
bazel_dep(name = "aspect_rules_js", version = "3.5.1")
bazel_dep(name = "aspect_rules_ts", version = "3.10.1")
bazel_dep(name = "rules_nodejs", version = "6.7.3")
```

These declarations alone do not pin interpreters or package dependencies:

- Python: register a hermetic rules_python toolchain with an exact patch version
  (H uses 3.12.12), archive URL/hash and supported platform. Derive a hash-checked
  requirements lock from the project's dependency intent and required test/build
  extras using a pinned resolver. Use the `pip.parse` Bzlmod extension with
  `python_version`, `requirements_lock`, `hub_name`, then `use_repo`; require
  hashes and pinned wheels where supported. Include local generated packages as
  declared source dependencies. Do not install into a shared editable environment.
- JS/TS: register an exact Node toolchain (H uses 24.10.0) and lock TypeScript and
  package-manager versions. Keep the project's existing package lock authoritative.
  For npm projects, derive the pnpm lock required by `npm_translate_lock` using a
  pinned importer; check package/version/integrity equivalence to package-lock.json.
  For existing pnpm projects, consume their lock directly. Declare workspace links
  and generator dependencies. Do not resolve a different dependency tree at test time.
- Runtime helpers: pin git, tmux, PostgreSQL and other binaries only when consumed;
  record archive checksums, platform/ABI and shared-library closure in a reviewed
  toolchain lock. Carry that closure in runfiles. Host PATH is not a fallback.

Resolve intentionally with `bazel mod deps --lockfile_mode=update`, review and
commit the lock, then check `bazel mod deps --lockfile_mode=error`. Keep error
mode in normal verification; regenerate locks from their sources, never hand-merge
them. A Bzlmod lock does not replace Python/npm/native locks. Prove minimal
Python and/or TS smoke execution in two independent clean roots before expanding
the graph. Unsupported platforms retain the legacy path with a clear diagnostic.

References: [Bzlmod locks](https://bazel.build/external/lockfile),
[rules_python pip.parse](https://rules-python.readthedocs.io/en/1.8.2/pypi/download.html),
[rules_js npm lock translation](https://github.com/aspect-build/rules_js/blob/v3.5.1/npm/private/npm_translate_lock.bzl).

## 3. Generate targets and an obligation manifest

Implement a reproducible repository-local generator, typically
`scripts/bazel/generate_targets.py`, with a drift/check mode. Emit stable sorted
BUILD files and `tools/bazel/test_suites.json` using the consumer schema discovered
in step 1. If no consumer exists yet, document the local manifest version and its
future adapter requirement; do not label an invented schema AQ-compatible.

Start with Python libraries per owning package and pytest targets per test module
through a small pytest macro/launcher. Declare imports, conftests/plugins, dynamic
loads, resources, migrations, prompts and generator inputs explicitly as deps/data.
Avoid a repository-wide library that invalidates every test for every edit.
For JS/TS, use rules_js/rules_ts targets for the existing test runner, typecheck,
build and generated-source dependencies; preserve each original command's options.
Do not invent a required JS test obligation when the project only gates typechecks.

The manifest must account for suite names, stable target labels, module/node or
command obligations, marker expressions/exclusions, tags, shards and service
requirements as supported by its schema. Include a complete aggregate suite.
AQ's aliases are `//checks:cli`, `:default`, `:postgres`, `:migration_slow`,
`:e2e`, `:dashboard`, `:cli_startup`, `:generated` and `:all`; use only applicable
equivalents for the project and map them explicitly. Verify inventory completeness
and generator idempotence. Wrap code generators as pinned actions in scratch
trees; compare tracked outputs byte-for-byte, and ignored outputs against an
independent locked generation. Never mutate the source checkout during a test.

## 4. Isolate targets, then shard deterministically

Run pytest serially inside each target; Bazel owns target concurrency. Construct
a positive child-environment allowlist with declared runfiles/tools, locale,
timezone and Bazel test variables. Put HOME, XDG/config, temp, git config and tmux
state under TEST_TMPDIR. Exclude inherited provider/GitHub/AQ credentials; local
auth tests generate disposable credentials. Do not change the parent worker's
environment or production harness credential handling.

Prevent external network access during execution while allowing private loopback
for declared services. Resolve downloads before test execution. Fail the hermetic
configuration if isolation is unavailable; use the explicit legacy path instead
of caching a host-dependent result. Database consumers own private clusters and
databases per target/shard, with pinned binaries, unique sockets/ports and bounded
process cleanup. Pure targets must not initialize a database. Preserve template
cloning and migration safety; never use or migrate the operator database.

Begin unsharded. For measured large modules, prove independent fixture/database/
scenario resets before setting `shard_count`. Match H's partition function:
normalize only the node ID's repository-relative path to POSIX form, preserve
test/parameter identity, then compute
`int.from_bytes(sha256(nodeid.encode()).digest(), "big") % shard_count`.
Never use Python's randomized `hash()` or collection order. Honor and validate
TEST_TOTAL_SHARDS/TEST_SHARD_INDEX, touch TEST_SHARD_STATUS_FILE and emit a receipt
even for a legitimately empty partition. Prove the union contains every expected
node exactly once, with no overlaps, across clean roots and reordered collection.
Use the [Bazel test environment contract](https://bazel.build/reference/test-encyclopedia).

Follow AQ's 60-second per-test budget including attributed setup/teardown, and
300-second whole-target budget including bootstrap and cleanup, with
`timeout = "moderate"` and an outer launcher deadline. Inject clocks where needed.
Timeouts fail and clean up owned processes. Explicitly classify demonstrated
flakes and disable their caching; do not hide them with retries. Uncacheable
quarantines remain visible and block the 100% cache parity gate.

## 5. Connect the existing AQ test backend

Use the installed backend's project/manifest configuration and validate module,
suite and marker mappings against generated labels. Once support is confirmed,
run a real mapped module, for example
`aq test --aq-backend bazel tests/test_core.py` (substitute an inventoried module).
Run the matching legacy command as well; where supported, select it explicitly
with `--aq-backend pytest`. An older AQ uses the unchanged original command.
JS-only checks require a supported suite/target mapping, not a fictitious pytest
module. If unavailable, report that connection as pending and keep npm usable.

On shared AQ hosts, all test execution uses AQ admission: the same box-wide slot,
full-suite lock when applicable, per-session worker/resource cap, cancellation
and accounting. Do not bypass it with direct Bazel or multiply xdist beneath
Bazel jobs. Direct Bazel execution is appropriate only on an isolated runner or
an already admitted harness. Unsupported filters/arguments must fail clearly
and show the equivalent legacy command, never silently discard `-k`, markers or
exclusions. Retain smart-selection shadow behavior; do not combine Bazel with
enforced smart narrowing unless the installed backend explicitly supports it.

## 6. Prove parity and graph validity before relying on Bazel

For every inventoried suite, run legacy and Bazel against the same exact commit,
platform, locks, environment contract and marker expression. Record JUnit plus
canonical node-ID/outcome sidecars with parameter IDs, xfail strictness and skip/
xfail reasons. Compare multisets of identities and outcomes, not totals. Reject
missing/duplicate nodes, collection/setup errors, absent shards and mismatched
SHAs; identical failing results do not establish a green suite. For JS/TS compare
each original obligation's result and generated artifact manifests too.

Use the installed V comparator if present, inspecting its help first; otherwise
provide a project-local equivalent. The reference interface is
`python scripts/bazel/compare_results.py --legacy legacy-results --bazel bazel-results --require-exact-sha`.
Retain Build Event Protocol (BEP), outcomes, lock/graph digests and timing evidence.
Repeat unchanged with a retained disk cache after discarding server/output state;
require zero test executions and identical result digests, not just a fast exit.
Record cold execution separately. Start with a local disk cache; keep untrusted
writers out of any cache used to establish protected verification.

Audit invalidation using deliberate fixture, resource, migration, package-init,
generator-input, deleted-file and renamed-file changes. Compare graph reverse
dependencies with the project's static impact/mandatory-check catalogue. Unknown
or unresolved dependencies widen to the whole affected suite; never choose the
smaller set without evidence. Record executed/eligible targets, warm/cold time and
slowest targets; unexplained global fanout needs repair.

AQ's later promotion gate requires 20 consecutive exact-dev samples per suite
without differences or missing evidence, 100% unchanged test-cache hits, budgets
met and no unresolved flakes/under-declaration. A local pair is preparation,
not that longitudinal proof. Leave CI and required checks unchanged; report
remaining evidence for a separately requested rollout.

## 7. Prepare train target signatures and hand off

Use the installed impact producer/consumer contract to map changed paths to
owning targets plus reverse-dependent test targets, queried from the same head's
graph. Bind evidence to project/repository/task/PR, source ref/head, merge-base,
target ref/SHA, policy revision, suite and graph/lock/generator digests. Include
both names for renames and the old path for deletions. Exclude generated outputs
only with a registry linking source inputs and verified exact-identity provenance;
generated-only edits, unknown paths or stale evidence remain universal conflicts.

T1 uses `signature_version: 1` under `train_signatures` in the batch
`artifact_snapshot` and candidate `source_manifest`. The AQ consumer owns freezing
these documents after identity rechecks through CommandHandler; do not write
database rows or fabricate attestations. Initial graph observations are
observational and cannot relax path conflicts. Missing/mixed versions or absent
consumers use conservative path selection and full checks. Record a supported
producer/consumer verification, or the precise missing connection; a standalone
impact JSON file alone is not a connected train.

Disjoint signatures never certify a candidate green. Every newly based train
candidate still invokes its complete exact-head gate; unchanged actions may hit
cache. Leave packing, parallel lanes, activation and CI trust policy to a separate
authorized rollout.

Finish with changed files, exact commands/results, coverage/parity discrepancies,
supported platforms, integration status and the working legacy fallback command.
State separately what is prepared, verified locally and still awaiting rollout.

## C++ projects

Use pinned `rules_cc` with an explicit compiler/linker/standard-library toolchain,
third-party BUILD definitions and declared generated headers/resources. Expect
more manual work translating CMake/ctest, platform flags, ABI and code generators;
a `rules_cc` dependency alone is insufficient. Compare ctest test identities and
outcomes with Bazel before relying on it. Hardware/GPU tests need explicit device
availability and no-cache behavior, not silent skips. Retain CMake/ctest fallback
and the same AQ admission, manifest, signatures and no-CI-cutover boundary.
