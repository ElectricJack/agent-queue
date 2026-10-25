# H5 pytest runtime contract, version 1

This implements the plugin boundary from `rev-bold-summit` r1. B1 owns its
integration into `pytest_main.py` and the BUILD graph; H2 owns environment and
process isolation; H3/H4 own private PG/E2E setup and cleanup. No legacy pytest
invocation loads this plugin automatically. No BUILD target is introduced here.

## Launcher integration

The merged H2 `run_isolated` and H3 `run_postgres_target` APIs currently return
an exit status, not a complete lifecycle receipt or an owned-process handle.
H3 already places PostgreSQL setup and teardown inside H2's isolated process.
B1 must adapt that lifecycle before using the callbacks below; it must not pass
a no-op `kill`, treat the H2 return code as a receipt, or start a fresh 300-second
clock inside PostgreSQL bootstrap. Pass only the outer deadline's remaining
time to nested launchers, allowing their termination grace periods, and measure
environment construction, process creation and final namespace reaping too.
H2/H3's default timeout alone does not certify that complete launcher interval.

1. Record `time.monotonic()` at launcher entry, before environment/PG/daemon
   bootstrap. Call `runtime.run_target(bootstrap, execute, cleanup, kill,
   started=entry_time)` in the parent launcher. Callbacks are asynchronous and
   must yield to the event loop. `execute` waits for the pytest **child process**;
   it must not invoke synchronous `pytest.main` on the parent's event loop.
2. Supply H2's cleanup for partial as well as successful bootstrap. The mandatory
   `kill` callback synchronously signals only the target's owned process groups
   with SIGKILL, without blocking. The wrapper reserves the last five seconds of
   the total 300-second deadline for graceful cleanup. Timeout cancels the pending
   operation without an unbounded cancellation wait, attempts bounded cleanup,
   then calls `kill`. Cleanup failure and target cancellation also call `kill`.
   Reaping and namespace teardown belong to H2; cancellation callbacks must not
   create more processes. Set Bazel `timeout = "moderate"` as the outer bound.
3. In the isolated child, construct `RuntimePlugin(output=sidecar_path,
   env=child_environment, target_tags=manifest_tags, shard_isolation=reset_evidence)`
   and pass it in `pytest.main(..., plugins=[plugin])`. The output directory must
   already exist under the target's declared outputs. Keep pytest serial. Disable
   pytest-timeout and retry/repeat plugins; conflicting plugins/timers fail closed.
4. Use one target per module initially. More than one shard requires a nonempty
   reference to verified independent fixture/database/scenario resets. Sharding
   does not make fixtures independent. B1 must derive the reference and tags from
   the reviewed manifest, not user-provided environmental claims.
5. Retain the returned target receipt **after cleanup**, even on failure, alongside
   the pytest JSON sidecar and JUnit. Return its `exit_status`. A missing sidecar,
   missing launcher receipt, external kill, or infrastructure failure is missing
   verification, never a green result. Bind invocation/commit/toolchain/suite
   metadata outside cached outputs in the V2/V3 evidence wrapper.

A per-test monotonic deadline starts before fixture setup and extends through
teardown. Shared setup is charged to the triggering node, and shared teardown to
the final node that releases it. Every node additionally lists shared fixture
setup durations by name and scope; these are a breakdown, **not additive costs**.
The `elapsed` value includes protocol overhead; phase values account for setup,
call and teardown. An expired deadline fails even beneath `xfail`. Pytest still
attempts registered finalizers after failure; the parent target deadline bounds
stuck cleanup. Linux SIGALRM interrupts stuck phases without waiting 60 seconds
in the regression tests. A fake monotonic clock and alarm seam exercise accounting.

## Shards and evidence

`normalize_nodeid` normalizes only the relative file path to POSIX form. It leaves
class/function/parameter text unchanged. Assignment is the big-endian SHA256 of
that UTF-8 node ID modulo `TEST_TOTAL_SHARDS`, preserving collection order within
each partition. Both shard index/count must be valid and accompanied by
`TEST_SHARD_STATUS_FILE`; the plugin touches that file during configuration.

Selection runs after pytest marker/keyword filtering. Every shard records the
same complete selected suite as `collected`, its partition as `selected`, and
one `nodes` entry per executed protocol. A genuine empty partition succeeds and
writes evidence; an empty suite, collection-only run, collection error, early
stop or repeated protocol cannot certify complete coverage.

Each node records canonical `nodeid`, `parameter_id`, `outcome`, `elapsed`,
`budget_exceeded`, `shared_setup`, `flake`, and `phases`. Each phase records
`duration`, native pytest `outcome`, normalized `result` (pass/fail/error/skip/
xfail/xpass), reason and xfail strictness. Setup/teardown errors and strict XPASS
stay distinguishable. Collection errors and runtime errors are explicit arrays.
The sidecar is atomically replaced, with `schema_version = 1` and final
`exit_status`. No timestamp, invocation ID or secret environment is copied in.

For each target/suite invocation, V2 calls `validate_shards(expected_nodes,
reports)` with independently supplied manifest nodes. It compares multisets,
shard indices, deterministic assignments, phase completeness, budgets and success.
It must additionally call `validate_target(receipt)` for **every shard**, then
`promotion_blockers(reports)`. None of these checks replaces exact-commit outcome
parity, cache evidence or the existing required checks.

## Flakes, coverage and timing inventory

A demonstrated flake uses `@pytest.mark.flaky(owner="...", evidence="...")` and
Bazel tags `flaky`, `no-cache`, and `external`. Set the Bazel `flaky` retry attribute
to **False** and attempts to one; `flake_contract(..., retries=0)` validates that
contract. `no-cache` excludes local/remote action caches; `external` additionally
forces execution when Bazel has a persistent test result. Neither `local` alone
nor `no-cache` alone proves that a test runs every invocation. The plugin rejects
known retry plugins and duplicate phase/protocol executions. Quarantines remain
executed, retain failing outcomes, and always produce promotion blockers, even
when an individual run passes.

`runtime-ledger.json` records the historical slow-test candidate and the exact
input digest, without claiming current phase measurements. No assertions,
modules or production clock seams were changed: there is no demonstrated need
for a split/trim or a production-clock refactor in this plugin task. The coverage
change ledger and demonstrated-flake ledger are therefore empty, not a claim
that the complete suite has no flakes. Any later split/trim must record old/new
node IDs, retained behavior/assertions and a regression showing the replacement
catches the old failing condition.

Full timing acceptance remains pending H3/H4 and B1 integration. On a quiet,
serial `perf_strict` runner, use the sidecars to inventory `elapsed > 60` and
`budget_exceeded` nodes, report setup/call/teardown plus shared setup, and validate
all launcher receipts including cleanup against 300 seconds. Do not use timing
measurements from a busy worker box as acceptance or replace the recorded
operator baseline with a fresh local run.

References: [Bazel test contract](https://bazel.build/reference/test-encyclopedia),
[Bazel common attributes](https://bazel.build/reference/be/common-definitions),
[pytest hooks](https://docs.pytest.org/en/stable/reference/reference.html).
