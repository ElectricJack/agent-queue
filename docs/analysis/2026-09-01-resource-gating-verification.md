---
tags: [analysis, ops, resources, testing, verification]
date: 2026-09-01
---

# Resource gating — manual verification, before and after

Measurements taken on the box the gating was built for, while it was
running its normal agent load. Implementation and configuration are
documented in [resource gating](../guides/resource-gating.md).

## The box

```
$ nproc
24
$ free -g | head -2
               total        used        free      shared  buff/cache   available
Mem:              31           5          10           0          16          25
```

24 cores, 31 GB, 8 concurrent agents (`project.max_concurrent_agents`).
Derived budget: `24 // 8 = 3` workers per session, 2 global test slots.

## Before — the failure reproduced live

The problem is not hypothetical and did not need staging. Mid-task, with
other agents doing ordinary work, the process table looked like this:

```
$ python -c "from src.resources.procs import pytest_processes, summarize_by_session, load_average; ..."
load (30.98, 15.50, 7.93)   pytest processes: 45
  slot-0 / crisp-summit   5
  slot-4 / nimble-apex    5
  slot-7 / noble-horizon  5
  slot-3 / prime-ember    2
```

with, among them:

```
3080162  pytest tests/ -n auto          # the entire 11k-test suite, uncapped
3169595  pytest tests/task_graph tests/test_database.py … -n auto
3185977  pytest tests/test_session_commands.py … -n auto
```

Three agents running `-n auto` at once, 1-minute load average 31 on a
24-core box, 15-minute average still climbing from 8. The `PYTEST_XDIST_
AUTO_NUM_WORKERS=4` stopgap in the vault harness files was the only thing
keeping that from being three times worse.

### Counting has to walk the process tree

The first version of the doctor check matched process command lines
containing `pytest` and reported **5** in the situation above. The real
number was **45**. An xdist worker is spawned through `execnet` and its
command line is:

```
python3.12 -u -c import sys;exec(eval(sys.stdin.readline()))
```

No "pytest" anywhere in it. A name match therefore reports one process per
run and misses the entire fan-out the check exists to catch.
`src/resources/procs.py:pytest_processes` matches controllers by name and
then adds their descendants; `tests/test_resource_doctor.py::
TestPytestProcessCounting` locks that in.

## After — one run, measured three ways

Same suite each time (`tests/test_session_spec.py`,
`tests/test_harness_parser.py` and the four new resource-gating files —
159 tests). The figure is the peak process count of *that run's own
subtree*, which is deterministic; box-wide counts are unusable while seven
other agents are working. Box `load1` either side is reported for context
only — it is other agents' noise, not this run's signal.

| | Peak procs in the run's subtree | Elapsed | Box load1 before/after |
|---|---|---|---|
| **A** — `pytest -n auto`, no gating | **26** | 7.8 s | 9.4 → 10.1 |
| **B** — `pytest -n auto` in a gated session | **5** | 4.5 s | 10.1 → 12.4 |
| **C** — `aq test` (slot + derived `-n 3`) | **6** | 4.8 s | 12.4 → 12.0 |

Reading the rows:

* **A → B is the whole fix, per session.** 26 processes to 5. Layer 1 is
  just an environment variable, and `-n auto` never sees 24 again.
* **The capped run is not slower — it is faster.** 7.8 s → 4.5 s. Worker
  startup dominates a short suite, and 24 workers on a contended box spend
  most of their lives waiting to be scheduled. The cap costs nothing here
  and only starts to cost anything on suites long enough to amortise 24
  process spawns, which is the case where the box could not afford them
  anyway.
* **C carries one extra process** (the `aq test` wrapper holding the flock)
  and one extra step: the `-n 3` it enforces is the derived share, not the
  harness stopgap's 4.

### Projected to the incident's shape

Eight agents testing at once, worst case:

| | Test processes box-wide |
|---|---|
| No gating (the 2026-09-01 incident) | 8 × 26 = **208** |
| Layer 1 only | 8 × 5 = **40** |
| Layers 1 + 2 (`aq test`, 2 slots) | 2 × 6 = **12** |

Layer 1 alone takes the box from "unusable" to "busy". Layer 2 is what
makes the number independent of how many agents are running, which is the
property that actually stops this recurring as the agent count grows.

## Semaphore behaviour under contention

Four concurrent `aq test` invocations against 2 slots:

```
[run1] aq test: slot 1 of 2, -n 3
[run4] aq test: slot 0 of 2, -n 3
[run2] aq test: waiting 0s for 1 of 2 test slot(s); held by prime-ember, prime-ember
[run3] aq test: waiting 0s for 1 of 2 test slot(s); held by prime-ember, prime-ember
[run2] aq test: slot 0 of 2, -n 3
[run3] aq test: slot 1 of 2, -n 3

$ aq test --aq-status
Test slots — 2/2 free
```

Two ran, two queued, both queued runs printed who was holding the slots
rather than sitting silent, and every slot was released at exit. The
"waiting" line matters as much as the gating: the daemon reads terminal
silence as a stall, so a blocked agent has to *look* blocked.

Crash release is covered by `tests/test_resource_semaphore.py::
TestCrashRelease::test_sigkill_returns_the_slot`, which `SIGKILL`s a real
holder subprocess and asserts the slot is free immediately afterwards with
its stale JSON record still on disk — no reaper involved.

## cgroups (layer 3) on this host

Not available, and the probe says why:

```
$ systemd-run --user --scope --quiet -p CPUQuota=100% -- true
Failed to connect to bus: No such file or directory
$ aq doctor --check resources.cgroups
info: hard per-session limits are off (resources.cgroups.enabled = false);
      env caps and nice still apply
```

This is WSL2 without a systemd user manager, so there is no slice to
delegate. `scripts/setup-cgroup-delegation.sh` is the one-time root step
for hosts that can support it; here layers 1 and 2 carry the load and the
daemon logs the reason once at startup rather than failing launches.

## Doctor output on a healthy box

```
$ aq doctor --check resources.load --check resources.test_pressure
ok    resources.load           5-min load 5.37 of 24 core(s)
ok    resources.test_pressure  7 pytest process(es), limit 24
```

Under the "before" conditions above the same checks report the 5-minute
load against the core count and name `slot-0 / crisp-summit`,
`slot-4 / nimble-apex`, `slot-7 / noble-horizon` with their process counts
— which is the question an operator asks next.

## What is not covered

* **Memory.** Every layer here bounds CPU and process count. The
  SIGKILLs seen on 2026-09-01 were memory pressure, and only layer 3's
  `MemoryMax` bounds that directly — on this host it is unavailable. Layers
  1 and 2 reduce memory use only as a side effect of running fewer
  processes.
* **Non-test workloads.** `npm`, `tsc`, `cargo` and friends are covered by
  `nice` and the thread-count environment variables, but nothing counts or
  gates them the way `aq test` gates pytest.
* **The projections are arithmetic**, not measured. Reproducing the
  208-process case would have meant deliberately saturating a box that
  seven other agents were working on.
