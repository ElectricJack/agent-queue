# Repeated daemon handler cost

Task: `prime-glacier-75`. Baseline: `fcbe9ca3d`, after the isolated Git cleanup
fix. The incident reported roughly 99% lifetime daemon CPU and a 150% peak.
Twenty-one minutes of command wall time included 935 seconds for 25 delivered-PR
checks, 876 seconds for 124 wait decisions and 821 seconds for 49 train ticks.
These durations overlap and include awaits; they do not attribute CPU.

## Findings and changes

- Delivered-PR observation initialized the retained Git repository before
  rejecting active writers, closed PRs or other ineligible inventory. It now
  rejects these first. Eligible proofs batch their two remote ref reads and
  retain fresh fetch/proof and apply checks.
- Wait usage sampling reparsed unchanged transcripts every retry, including
  acknowledged continuations without completed usage. A bounded, stat-validated
  cache now retains missing observations as well as token counts. Parsing and
  token reduction both run off the event loop. File changes invalidate entries,
  and completed cost accounting stops writing repeated measurements.
- Train stall scans recorded their cadence only after awaiting the inventory.
  Overlapping ticks could repeat that work. The cadence is now reserved before
  yielding, and a single-flight guard covers scans longer than one minute.

## Repeatable isolated comparison

Run the same script against baseline and changed source with identical flags:

```bash
python scripts/acceptance/daemon-handler-cost.py --records 100000 --repeats 10
```

The script uses a temporary real bare Git repository and a synthetic Claude
transcript, fixed database/GitHub doubles and no daemon or credentials. CPU is
process CPU time, including worker threads. Wall time is elapsed time. The
baseline was measured before editing the handlers; raw outputs were retained
locally as `/tmp/prime-glacier-before.json` and `/tmp/prime-glacier-after.json`.

| Workload, ten repetitions | Before | After |
|---|---:|---:|
| Active PR preview Git calls | 30 | 0 |
| Active PR preview wall time | 76.83 ms | 0.12 ms |
| Active PR preview process CPU | 29.08 ms | 0.12 ms |
| Unchanged 100,000-record transcript full reads | 10 | 1 |
| Transcript process CPU | 9.955 s | 1.373 s |
| Transcript wall time | 9.458 s | 1.304 s |

These measurements demonstrate avoided work for these workloads. They do not
measure production daemon CPU, network lock contention or the frequency of
these cases in the live fleet. The single-flight train change is verified with
overlapping ticks and cancellation barriers, without a timing benchmark.

## Regression verification

With `POSTGRES_TEST_DSN` set to the disposable service on port 5534:

```bash
aq test tests/test_integration_pr_delivery.py tests/test_integration_train.py \
  tests/test_wait_hibernation.py tests/test_wait_faults.py \
  tests/test_integration_delivery_watchdog.py \
  tests/test_integration_delivery_watchdog_regressions.py \
  tests/test_selection_catalogue.py -x
```

Result: 453 passed. Coverage includes PR proof/apply races, cache invalidation
and eviction during file checks, off-thread reduction, overlapping/cancelled
train inventories, wait recovery and delivery watchdog behavior. Changed Python
files pass Ruff; the selection catalogue was regenerated.

## Live acceptance

Attaching `py-spy` to the daemon was denied by the host's ptrace policy. The worker
did not change host permissions, deploy or restart the operator daemon.
Supervisor reply `msg-4feb0206ae2d4485a3786f7a8a8235cf` on
`prime-glacier-profile` accepted isolated measurements for the implementation
and assigned deployment and CPU soak to the supervisor. The incident's
below-30% idle CPU target under normal load remains a live acceptance check;
this report does not claim that target has been reached.
