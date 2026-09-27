# Tasks and Agents cold readiness on main

Task `solid-meadow`, claim epoch 1. wise-ember.17's matched pair reported loaded
cold p95 Tasks 2791/3538 ms, Agents 3599/1914 ms and pane 210/272 ms for client
settings 1/3 ([report](2026-09-26-metrics-progressive-cold-readiness.md)). This
run measured current main under the same protocol before changing any code, as
the supervisor directed. **Main meets every cold-readiness and pane target in both
modes and both client settings, so no code change ships.**

## Why the earlier pair missed

The pair that filed this task measured `1c5bcb351`. That revision contains
neither of wise-ember.16's daemon fixes: `51abdeaa2` (the roster and the agent
reconciler no longer hydrate all 10,000 tasks) and `2663a585e` (the startup heap
is frozen, so gen-2 collections stop stalling the loop). Both were already on
main (`git merge-base --is-ancestor`). Every roster read and every reconciler
tick in that run still built the whole table. That run's browser-active loop
p95 was 184/193 ms, with more than 600 stalls over 500 ms in each mode.
`agent/list` had a median of 0.3–1.4 s. wise-ember.16's own pair, whose target
carried those fixes but not wise-ember.17's bundle, already met the Tasks, Agents
and pane targets. No run had yet combined the two.

## Target and protocol

- **Target and runner:** `510142de7`, origin/main after `clear-delta` delivered
  wise-ember.17. It contains `51abdeaa2`, `2663a585e` and `1c5bcb351`.
  - Verified 121-file release bundle.
  - Isolated daemon at :8099, edge at :8092, both run from the task's worktree.
- **Database:** `localhost:5534/agent_queue_e2e`, separate from the operator
  database.
  - Upgraded `a00000000033` → `a00000000037` under supervisor authorization,
    only this database.
  - The fixture is unchanged across the upgrade: PAUSED `perf-fixture`, 10,000
    READY tasks, 0 live sessions, identical task digest.
- **Warm-up:** one hour of metrics history (3524 one-second rows over 3596 s,
  largest gap 2 s).
- **Commands:** the prescribed `experiment.py` commands, idle then loaded:
  three repetitions, clients 1,3, 30 s warm-up, 120 s observation, surfaces
  tasks,agents,metrics.
  - The same `load.py` arguments (1800 s, 2×10¹¹ iterations, 128 GiB IO,
    256 MiB cap, four workers).
  - Chrome 150 at 1600×1000.
- **Caps and niceness:** baseline caps 3/3/2, parent nice 10, helper nice 19.
  No exclusive box lock, reset or daemon migration.
- **Match with the original baseline:** the manifests match wise-ember.12's in
  every field except the revision and the worktree path of `load.py`.

## Cold readiness and task pane

p95 is nearest-rank, the maximum of three cold loads or six pane opens. Spread is
(max−min)/median. Target: cold ≤ 2000 ms, pane ≤ 200 ms.

| Mode | Clients | Surface | Raw ms | p95 | Spread | wise-ember.17 p95 |
| --- | --- | --- | --- | --- | --- | --- |
| idle | 1 | Tasks | 1194, 774, 738 | 1194 | 0.589 | 3506 |
| idle | 1 | Agents | 546, 914, 609 | 914 | 0.604 | 2718 |
| idle | 1 | Metrics | 487, 458, 480 | 487 | 0.060 | 775 |
| idle | 3 | Tasks | 770, 742, 976 | 976 | 0.304 | 2342 |
| idle | 3 | Agents | 1416, 655, 669 | 1416 | 1.138 | 2183 |
| idle | 3 | Metrics | 465, 451, 522 | 522 | 0.153 | 857 |
| loaded | 1 | Tasks | 1066, 1578, 915 | 1578 | 0.622 | 2791 |
| loaded | 1 | Agents | 623, 1504, 632 | 1504 | 1.394 | 3599 |
| loaded | 1 | Metrics | 540, 559, 491 | 559 | 0.126 | 881 |
| loaded | 3 | Tasks | 800, 1064, 1256 | 1256 | 0.429 | 3538 |
| loaded | 3 | Agents | 624, 699, 584 | 699 | 0.184 | 1914 |
| loaded | 3 | Metrics | 482, 635, 488 | 635 | 0.314 | 1665 |

| Mode | Clients | Pane raw ms | p95 | Spread | wise-ember.17 p95 |
| --- | --- | --- | --- | --- | --- |
| idle | 1 | 104, 53, 104, 60, 109, 58 | 109 | 0.683 | 226 |
| idle | 3 | 118, 55, 103, 60, 113, 69 | 118 | 0.733 | 314 |
| loaded | 1 | 101, 73, 187, 100, 110, 48 | 187 | 1.383 | 210 |
| loaded | 3 | 117, 74, 122, 64, 115, 62 | 122 | 0.635 | 272 |

The tightest margins are loaded Tasks at client setting 1 (1578 ms) and the
loaded pane at client setting 1 (187 ms).

## Direct reads

These are 90 samples per route, per mode and client setting, with zero errors
on every route.

| Mode | Clients | agent/list p95 | pool/status p95 | task/get p95 | task/list p95 | metrics/series p95 |
| --- | --- | --- | --- | --- | --- | --- |
| idle | 1 | 100.2 | 67.1 | 43.2 | 1007.8 | 36.9 |
| idle | 3 | 28.3 | 104.0 | 49.5 | 627.5 | 16.0 |
| loaded | 1 | 43.3 | 145.0 | 45.9 | 1059.2 | 87.8 |
| loaded | 3 | 54.4 | 228.1 | 62.2 | 973.8 | 147.3 |

The roster meets its target in both modes: agent/list p95 ≤ 500 ms and loaded no
more than 2× idle. `task/list` is the whole 10k-task list, not a small read.

## Daemon histograms, workload and ambient load

Browser-active scope, with histograms merged by addition:
- **Loop p95:** idle 26.80 ms and loaded 43.91 ms, against 184.48 and 192.95 ms
  in wise-ember.17's pair.
  - Per-repetition loaded: 47.37, 49.55 and 32.59 ms (spread 0.358).
  - Per-repetition idle: 23.72, 20.34 and 35.57 ms.
- **API p95:** idle 450.65 ms and loaded 703.19 ms.
- **Relay p95:** idle 436.21 ms and loaded 626.79 ms.
- **Query p95:** 11.35 and 19.08 ms. **Pool-wait p95:** 0.95 ms.

The loop maximum still misses: 2648 ms idle and 3771 ms loaded, with 24
browser-active stalls over 500 ms in each mode. This is the residual
wise-ember.16 disclosed and attributed to `GET /api/metrics/series`. It is not
part of this task's cold-readiness acceptance and is not changed here.

Every loaded repetition finished all 137,438,953,472 IO bytes and reached the
1800 s CPU deadline:

| Repetition | Completed CPU iterations | Helper-only tail |
| --- | --- | --- |
| 1 | 70,570,100,000 | 777 s |
| 2 | 75,788,400,000 | 772 s |
| 3 | 86,677,700,000 | 796 s |

Median throughput was 42,103,034.8 iterations/s (spread 0.213). The original
baseline had 41,646,220.6 and wise-ember.17 had 33,100,606.4. Every browser and
direct-API window has coverage fraction 1.0.

Passive `/proc` records every 5 s. The host has 24 cores.

| Mode | Load1 median / p95 / max | CPU some PSI avg10 median / p95 / max | Memory some max | IO some max |
| --- | --- | --- | --- | --- |
| idle | 4.31 / 9.51 / 13.06 | 0.02 / 1.36 / 4.99 | 0.39 | 2.45 |
| loaded | 13.36 / 23.17 / 26.82 | 0.55 / 8.90 / 16.38 | 5.17 | 5.51 |

Ambient contention was well below wise-ember.17's pair and above the original
baseline's:

| Loaded measure | This pair | wise-ember.17 | Original baseline |
| --- | --- | --- | --- |
| Passive CPU PSI p95 | 8.90 | 30.75 | — |
| Sampler's browser-active CPU some-PSI max | 10.46 | 38.41 | 6.18 |
| Ungated-process max | 24 | 36 | 11 |

The ambient difference and the missing daemon fixes both cut against
wise-ember.17's numbers. This pair does not separate their shares. It shows that main now meets the targets under the
prescribed load. Loopback measurement is not a LAN or tailnet result.

## Offline profile and the unshipped client change

Before the window opened, an offline diagnostic was run: the production bundle
against a stubbed API serving the real 10k-task graph payload read-only.

With zero API delay, the client cold path is CPU-bound, not waiting:
- At 1× CPU, Tasks takes about 450 ms and Agents about 430 ms.
- Row-count work, including the 2.32 MB graph parse and the virtualizer
  measuring 10k rows, is about 150 ms of the 1.1 s at 4× CPU.
- The server builds the graph in about 70 ms quiet:
  - PostgreSQL 5.7 ms
  - SQLAlchemy rows 35 ms
  - models 27 ms
  - dump 7 ms

Commit `977d35943` on `aq/solid-meadow` does three things:
- starts the initial route's lazy chunk chain up front
- prefetches `pool/status` for `/agents`
- loads xterm only when a terminal view opens

Measured offline:
- Tasks median at 4× CPU: 1127 → 997 ms.
- Agents median with emulated daemon latencies: 640 → 530 ms.
- Tasks is unchanged when the graph read is the long pole.

It was reverted on the branch (`01e5382ed`) because main already meets the
targets. It stays in history if these margins erode. A same-process A/B of a
cheaper graph row build showed no gain (36.1 against 35.7 ms), and it was
dropped.

## Evidence

Artifacts are in
`~/.agent-queue-e2e/dashboard-perf/2026-09-27/solid-meadow/claim-epoch-1/`:

- **Upgrade:** `upgrade-before.json`, `upgrade-after.json`,
  `upgrade-alembic.log` and `upgrade-isolated.py`.
- **Pair (`main-pair/`):**
  - `preflight.json`, `bundle-manifest.json`, `target.json`, `dashboard.json`,
    `warm-history.json`
  - `pair-status.json` with the exact commands; both modes exited 0
  - `idle/` and `loaded/` (manifests, harness JSON, series, load, inventories,
    summaries)
  - `pair-analysis.json`, `ambient.jsonl`, `heartbeat.log`
- **Cleanup:** `cleanup.json` records only the owned target and edge stopped
  and ports 8092 and 8099 free.
- **Hashes:** `artifact-manifest.json` hashes the retained files.
