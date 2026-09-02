---
tags: [guide, ops, resources, testing]
---

# Resource gating — keeping N agents from taking the box down

On 2026-09-01 this repo's own daemon ran eight concurrent agents on a
24-core / 31 GB box. Each of them was following `CLAUDE.md`'s advice to run
`pytest -n auto`. `-n auto` asks the *machine* how many cores it has, and
every agent got the same answer, so eight agents became up to **192 test
processes**: load average past 60, memory pressure, and sessions killed by
the OOM reaper mid-task.

Nothing was misconfigured. The failure is structural: every agent sizes its
own work against the whole box, and no agent can see the other seven. The
fix is to make the box's capacity something the agents are *told*, not
something they measure.

Three layers do that, and each works on its own.

| Layer | Bounds | Enforced by | Needs root |
|---|---|---|---|
| 1. Session env caps + `nice` | what one session's tools *think* they may use | the launcher, at spawn | no |
| 2. `aq test` semaphore | how many test runs happen at once, box-wide | `flock` | no |
| 3. cgroup scopes | what a session *can* use, cooperative or not | the kernel | once |

Layers 1 and 2 are cooperative and on by default. Layer 3 is the backstop
for the processes that ignore the first two, and it is opt-in because it
needs a one-time privileged step.

---

## Configuration

Everything lives in one section of `~/.agent-queue/config.yaml`. The
defaults below are the shipped ones.

```yaml
resources:
  enabled: true
  # Physical budget. null → os.cpu_count().
  cores: null
  # How many agents this box is expected to run at once. This is the
  # denominator of the per-session share, so it should match the largest
  # project's max_concurrent_agents.
  max_concurrent_agents: 8
  # Explicit override for the per-session share. null → cores // agents.
  per_session_cpu_share: null
  # nice increment for the harness process. 0 disables.
  session_nice: 10

  # Layer 2 — the global test semaphore.
  test_slots: 2
  test_workers: null            # null → the per-session share
  test_wait_timeout: 1800
  test_poll_interval: 2.0
  test_deselect_markers: "not perf and not migration and not slow and not tmux and not integration"

  # Doctor thresholds.
  load_warn_ratio: 1.0          # warn when 5-min load > cores × this
  max_pytest_processes: 24

  # Layer 3 — hard limits. See "cgroups" below.
  cgroups:
    enabled: false
    cpu_quota_percent: 600
    memory_max: 6G
```

The section is hot-reloadable: the launcher reads it per launch and `aq
test` reads it per run, so a change takes effect on the next session
without a daemon restart.

On the 24-core box above, the defaults derive **3 workers per session** and
**2 concurrent test runs**, which bounds the worst case at 6 test processes
instead of 192.

---

## Layer 1 — session env caps and `nice`

`src/sessions/spec.py` folds `session_env_caps()` into every launch, so the
session's environment carries:

| Variable | What it stops |
|---|---|
| `PYTEST_XDIST_AUTO_NUM_WORKERS` | `-n auto` resolving to the core count |
| `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `NUMEXPR_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS` | BLAS backends spawning one thread per core *per process* |
| `UV_THREADPOOL_SIZE` | libuv (and therefore Node, and therefore the Claude CLI) sizing its blocking pool from the core count |
| `AQ_CPU_SHARE`, `AQ_CPU_CORES`, `AQ_TEST_SLOTS`, `AQ_TEST_WORKERS` | not caps — these tell `aq test` inside the worktree what the daemon derived |

The harness process is then launched under `nice -n 10`. That is the
cheapest half of the whole fix: the agents still get the CPU when it is
free, but the daemon, the dashboard, the API and tmux stay schedulable when
it is not, which is the difference between "the box is slow" and "the box
is unreachable".

**An operator-pinned value always wins.** If a key appears in a harness's
`env` block in `vault/harnesses/<id>.md`, the derived cap is not applied to
it. That is how the original stopgap (`PYTEST_XDIST_AUTO_NUM_WORKERS: "4"`)
keeps working: to switch a box over to the config-derived number, delete
that line from the harness file.

---

## Layer 2 — `aq test`

Layer 1 bounds each session. It does not bound the sum: four workers each
is still 32 processes when eight agents test at the same moment, and test
runs are exactly the bursty, everyone-at-once workload that produces the
spike. `aq test` is a pytest wrapper that takes one of `test_slots` slots
first.

```bash
aq test tests/test_pools.py              # one file, still slot-gated
aq test tests/ -k claim                  # a slice of the suite
aq test --aq-status                      # who is holding the slots
aq test --aq-no-wait tests/              # fail instead of queueing
aq test --aq-dry-run tests/              # print the pytest command
aq test --aq-help                        # this help (-h belongs to pytest)
```

Everything that is not an `--aq-*` option goes to pytest untouched. The
wrapper adds `-n <cap> --dist loadfile` and the default marker deselects **only when you
did not pass your own** — `aq test -m perf tests/perf` and `aq test -p
no:xdist tests/` both do exactly what they say.

While it waits, it prints a line per poll naming the current holders. That
is deliberate: the daemon reads terminal silence as a stall, so an agent
queued behind a busy box has to *look* queued.

Exit codes are pytest's, with one addition: **75** (`EX_TEMPFAIL`) means no
slot came free within `test_wait_timeout`. That is "come back later", not
"your tests failed".

### Why `flock`

The slot is held by an open file descriptor on
`{data_dir}/locks/test-slots/slot-N.lock` (by default,
`~/.agent-queue/locks/test-slots/slot-N.lock`). The kernel drops the lock
when the last descriptor closes — clean exit, `SIGKILL`, OOM kill,
`tmux kill-session`, all of them — so a crashed holder needs no reaper.
Holder metadata written into the file is advisory only: every reader
re-tests the lock rather than trusting the JSON, so a dead holder's stale
record can never make a free slot look busy.

It also works with the daemon down, which matters because `aq test` runs
inside worktrees during restarts, and a test wrapper that fails closed when
the daemon is unavailable would simply be routed around.

---

## Layer 3 — cgroup v2 scopes (optional)

Layers 1 and 2 are cooperative. A script that hardcodes `-n 24`, or a build
that spawns per-core regardless of `OMP_NUM_THREADS`, still gets to take
the box down. Layer 3 launches each session inside a systemd scope with a
`CPUQuota` and a `MemoryMax` the kernel enforces whether the process
cooperates or not.

Creating such a scope is privileged unless the daemon user's slice has
`Delegate=yes`, which only root can set, and only once:

```bash
sudo scripts/setup-cgroup-delegation.sh          # defaults to $SUDO_USER
```

Then enable it and restart:

```yaml
resources:
  cgroups:
    enabled: true
    cpu_quota_percent: 600   # six cores per session
    memory_max: 6G
```

```bash
./run.sh restart
aq doctor --check resources.cgroups
```

**Absence degrades, it never blocks a launch.** The daemon probes
delegation once at startup, logs the reason if it is missing, and falls
back to layer 1. `resources.cgroups` reports `info` when layer 3 is off and
`warn` when it is switched on but not actually working — the latter being
the dangerous state, where an operator believes hard limits are protecting
them and they are not.

On WSL2 and in containers, delegation is frequently unavailable and the
script will say so. That is a supported configuration; layers 1 and 2 carry
the load.

---

## Rolling it out on an existing install

Layers 1 and 2 are on by default — there is nothing to add to
`~/.agent-queue/config.yaml` unless you want to change a number. Two
deployment steps do need a human, because both touch live shared state:

1. **Retire the stopgap.** `vault/harnesses/claude.md` and
   `vault/harnesses/codex.md` may still carry
   `"PYTEST_XDIST_AUTO_NUM_WORKERS": "4"` in their `env` block from before
   this existed. It still works — an operator-pinned key always wins — but
   it pins one harness to a number that no longer tracks the box. Delete
   the line from both files to let the derived share apply; the vault
   watcher picks it up live, no restart.
2. **Pin the denominator.** `resources.max_concurrent_agents` defaults to
   `8`. If your project's `max_concurrent_agents` differs, set it to match,
   or the derived share will be wrong in whichever direction the two
   disagree.

Then confirm the launcher is doing it — the daemon states the budget once
at startup:

```
Resource gating: 24 core(s) / 8 concurrent agent(s) -> 3 worker(s) per
session, nice +10, 2 global test slot(s)
```

## Diagnosing a saturated box

```bash
aq doctor --check resources.load
aq doctor --check resources.test_pressure
aq test --aq-status
```

| Check | Fires when | Reports |
|---|---|---|
| `resources.load` | 5-min load > `cores × load_warn_ratio` | the load figures plus the pytest processes per session |
| `resources.test_pressure` | more than `max_pytest_processes` pytest processes box-wide | the count and which sessions own them |
| `resources.cgroups` | always | whether hard limits are actually in force |

The load check reads the **5-minute** average on purpose. A 1-minute spike
is a build starting; five minutes above one runnable task per core is a box
where every agent is now slower than it needs to be and the OOM killer is
the next event.

Both checks attribute processes back to sessions from `/proc`: `AQ_TASK_ID`
and `AQ_SESSION_NAME` from the process environment (set on every launch and
inherited by everything the harness spawns), falling back to the worktree
slot in the process's `cwd`. A finding therefore names `slot-3 /
prime-ember (96)` rather than an anonymous number, because "which session?"
is always the operator's next question.

---

## Verification

[The verification note](../analysis/2026-09-01-resource-gating-verification.md) records the
before/after load numbers measured on the box this was built for.

Unit coverage:

```bash
aq test tests/test_resource_limits.py tests/test_resource_semaphore.py \
        tests/test_resource_doctor.py tests/test_cli_test_runner.py
```
