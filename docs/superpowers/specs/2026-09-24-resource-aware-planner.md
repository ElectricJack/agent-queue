# Resource-aware planner — preliminary spec

**Date:** 2026-09-24 · **Status:** preliminary — analysis and options, no decision taken · **backlog (long-term)**
**Roadmap:** [operator feedback roadmap](2026-09-24-operator-feedback-roadmap.md)
**Related:** [exclusive job queue](2026-09-24-exclusive-job-queue.md) (hard prerequisite) ·
[agent sleep/wake](2026-09-24-agent-sleep-wake.md) ·
[smart test selection](2026-09-24-smart-test-selection.md) ·
existing: [usage-aware concurrency](2026-08-24-usage-aware-concurrency.md) (historical),
[global worker pools](2026-09-08-global-worker-pools-design.md),
[provider usage](2026-09-07-provider-usage-design.md),
[scheduling concepts](../../concepts/scheduling.md),
[resource gating](../../guides/resource-gating.md),
[fleet metrics](../../specs/design/fleet-metrics.md)

## 1. The ask

The long-term goal is resource-aware planning. It would be a Gantt-style
scheduler that treats **CPU and memory as resources**, alongside the agent
slots and provider quota that are budgeted today. Work would be placed in time
according to what it is predicted to consume, instead of admitted whenever a
seat is free. The operator framed this as long-term. It depends on the
categorisation and duration data that the
[job queue](2026-09-24-exclusive-job-queue.md) would start producing.

## 2. What exists today

### 2.1 Concurrency is counted in seats, not in resources

- **Push path.** `Scheduler` in `src/scheduler.py` (L273) is a pure,
  deterministic fair-share allocator. It sorts by token-deficit against
  `credit_weight`, gives a min-task guarantee, and respects
  `project.max_concurrent_agents` (L428), budgets and workspace locks. It
  returns `AssignAction`s. It has no notion of CPU, memory or duration.
- **Pool path.** `size_pools()` in `src/scheduler.py:646` is a pure function.
  It computes `want = busy + ready`, clamps that to the profile's
  `min_active`/`max_active`, and hands out starts round-robin under
  `global_cap`. `global_cap` comes from `Orchestrator._pool_global_cap()`
  (`src/orchestrator/pools.py:403`), which returns `swarm.global_max_active`
  or, failing that, `resources.max_concurrent_agents` (default 8,
  `src/config.py:2194`). `place_pool_actions()` (L836) then picks a project
  from its quarantine, workspace capacity, project cap and `min_per_project`
  inputs. Unplaceable work surfaces as `pool.placement_starved`.
  (CLAUDE.md places these in `src/orchestrator/scheduler.py`. They actually
  live in `src/scheduler.py`.)
- **Machine resources are only a static divisor.**
  `ResourcesConfig.cpu_share()` (`src/config.py:2224`) is
  `cores // max_concurrent_agents`. It feeds env caps, `nice` and the xdist
  worker count through `src/resources/limits.py`. `test_slots` bounds
  concurrent `aq test` runs through a flock (`src/resources/semaphore.py`).
  `src/config_tuning.py` (`MachineResources.detect()`, `recommended_tuning()`)
  derives those numbers from cores and RAM **once**, at install or when
  `aq system config tune` runs. The only runtime reaction to load is the
  doctor's (`resources.load`, `resources.test_pressure` in
  `src/doctor/resource_checks.py`), and it only reports.
- **Quota is handled as availability, not as reservation.** Provider
  availability (`src/providers/availability.py`, `availability_service.py`)
  flips a provider to `degraded` past `usage.degraded_percent` (default 85,
  `src/config.py:2460`) and to unavailable when exhausted. Scheduling then
  suppresses launches or holds the work. The usage-aware concurrency spec's
  Phase C `recommended_concurrency()` was **not built** (no match in `src/`).
  Quota is a gate, not a forecast.

### 2.2 The data a planner could use

| Source | What it has | Gap for planning |
|---|---|---|
| `metrics_samples` (`src/metrics/sampler.py`, `read_machine()` L86) | `load1/5/15`, `cpu_count`, memory totals every 1 s. Kept for 1 h at 1 s, 30 d at 1 m, 1 y at 1 h | Box-wide only. Not attributed to a session, task or job. No PSI and no I/O |
| `src/resources/procs.py` (`scan_processes`, `summarize_by_session`) | Per-session process attribution from `/proc` | On demand (doctor). Nothing persists it |
| `task_session_attempts` (`src/database/tables.py:1261`) | Per-attempt `started_at`/`ended_at`, profile, intelligence class, model, outcome | Wall time only. No CPU or memory. Duration of *agent* work, not of its heavy commands |
| `token_ledger` | Token spend per task, model and agent | Tokens, not machine resources |
| Job history (proposed in [job queue](2026-09-24-exclusive-job-queue.md) Phase 1–2) | Per-command class, wait, duration, CPU-seconds, peak RSS | **Does not exist yet** |

Tasks carry no size or duration estimate. `tasks` has no estimate column, and
`aq-graph`/formula nodes declare none.

## 3. Gaps

1. **Resources have no forward view.** Every decision is "is a seat free
   *now*?". Nothing asks whether starting this work pushes the box past its
   memory or CPU budget over the next N minutes.
2. **The unit of heavy consumption is invisible.** An agent's harness is
   mostly idle, waiting on an LLM. Load comes in bursts from what it runs:
   tests, builds, e2e. The scheduler admits *agents*, while the load is
   caused by *commands*, which today are gated only by `test_slots`.
3. **No duration or size estimates** for tasks or commands. A Gantt chart
   without durations is a list.
4. **No attribution in the time series.** Attributing observed load to
   sessions or jobs at 1 s would need `procs.py`-style scans. Their cost
   matters: the sampler's design constraint is that it "must not make the box
   it is measuring slower" (sampler.py docstring).
5. **Static limits.** `max_concurrent_agents` and `test_slots` are
   hand-tuned or derived once. Nothing adapts them to observed headroom.

## 4. Implementation options

### Option A — Admission control by predicted load

**Sketch.** Before a pool start, a push assignment or a job start, a pure
`admit(candidate, forecast)` checks a short-horizon forecast. The forecast is
current load plus the predicted footprint of work already admitted, compared
against budgets (`cores × target_ratio`, `mem_available − reserve`). There is
no time axis beyond "now plus decay". The footprint comes from the job
queue's per-class and per-signature history, with defaults per class until
that data exists. Held work gets a derived hold reason, like
`provider_hold`: never stored, and explained by `aq task explain`.

**Touches.** `size_pools` or `place_pool_actions` (a new input: headroom), the
push `SchedulerState`, the job queue's start policy, `aq task explain`, and
metrics (a headroom series).

**Pros.** Small. It fits the existing pure-function-plus-snapshot pattern and
the derived-hold idiom. It also degrades well: with no history it falls back
to today's static caps.

**Cons.** Myopic: it cannot plan a quiet window for a perf run or line up
exclusive jobs. Admitting an *agent* on predicted *command* load is indirect.

**Size:** M.

### Option B — Adaptive caps (feedback control)

**Sketch.** Close the loop on measured signals instead of predictions. A
controller adjusts the effective `global_cap` and `test_slots` between floor
and ceiling bounds using load5, PSI and memory pressure, with hysteresis.
This is the usage-aware spec's Phase C shape ("may only lower the static
cap"), applied to CPU and memory instead of quota.

**Touches.** `_pool_global_cap()`, the job queue's slot count, metrics (an
effective-cap series), and config (bounds and gains).

**Pros.** Needs no duration model. It is robust to bad estimates, and cheap.

**Cons.** Reactive: it throttles after the spike. It can oscillate if tuned
badly. It still has no time axis.

**Size:** S–M.

### Option C — A time-indexed reservation schedule (the Gantt)

**Sketch.** Keep a rolling reservation table over, say, the next 2–4 hours in
coarse slots of 1–5 minutes. Each queued job and task gets an interval, sized
from its estimated duration, and a resource vector (cores, memory, agent
seat, provider quota share). A pure planner re-solves on each cycle or on
each event, using list scheduling with priorities; a greedy heuristic is
enough, since optimality is not the goal. Its output is start times, and the
executor only admits work whose reserved start has arrived.

That makes it possible to express "reserve a quiet window at 03:00 for perf
budgets", "exclusive jobs back to back", "do not start a deep-high task that
will want the full suite in 20 min when the full-suite lock is booked" and
"spend quota before its reset". The dashboard would render the schedule as a
Gantt chart.

**Touches.** Everything in A, plus a planner module, a reservation store
(in memory and derived, or durable), a UI, and estimate models for both tasks
and jobs.

**Pros.** The full vision. It unifies CPU, memory, seats and quota, and gives
explainable ETAs.

**Cons.** Needs estimates this system does not have yet. Agent task durations
are high-variance, and LLM work is not a batch job. There is a risk of a
beautiful chart that is wrong. Plans go stale on every early finish and need
constant replanning.

**Size:** XL.

### Option D — Staged: A (+B) now, C's data model later

**Sketch.** Build B's controller as the safety net and A's admission check on
top of it. Record *predicted vs actual* footprint and duration for every job
and attempt from day one. Only when that record shows predictions are good
enough (see Q2) do we promote to C, starting with jobs alone, which are far
more predictable than agent tasks.

**Size:** M now, XL eventually.

## 5. Initial take

Provisionally **Option D**, and explicitly **backlog**. Nothing here should
start before the [job queue](2026-09-24-exclusive-job-queue.md) Phase 1–2
history exists and before Phase 0's measurement has said which resource
actually binds (CPU, memory or I/O). The first increment worth building is
probably **B for jobs only**: the job queue's slot count adapts to PSI and
load. That delivers most of "CPU and memory as resources" at S–M cost. It
also keeps the scheduler's agent seats static, and that static count is the
thing operators currently reason about. The Gantt view should arrive, if it
arrives, as a read-only projection first ("what will run when, given
current estimates"), which is useful for supervisor narrative updates, before
it drives any decisions.

## 6. Open questions

1. **Which resource actually binds on the operator's box: CPU, memory, disk
   I/O or provider quota?** This decides what the planner's resource vector
   must contain. Answer it with the job queue's Phase 0 PSI sampling before
   designing anything.
2. **How predictable are durations?** Job durations are probably predictable
   from signature history. Agent task durations from
   `task_session_attempts`, by class, profile or formula, may not be. What
   error bound makes a reservation better than admission control? This
   decides whether Option C is ever justified.
3. **What is the unit of planning: agents, or the heavy commands they run?**
   If load is dominated by jobs, planning jobs alone, while agents stay
   seat-bounded, may capture nearly all the value.
4. **Is quota in scope?** Folding provider quota into the same vector puts
   the planner in charge of what `provider-failover` and availability do
   today. Should it plan against the provider usage windows, or treat
   availability as an external constraint?
5. **Should the plan be durable?** A derived schedule, recomputed every cycle
   and never stored like provider holds, is simpler and restart-safe. A
   durable one supports promised ETAs and operator edits, such as "reserve
   03:00 for perf".
6. **What overrides exist?** Can an operator or supervisor book a window
   ("quiet box 02:00–02:30"), and is that a playbook action (timer, cron) or
   a command?
7. **How much sampling is affordable?** Per-job attribution is cheap through
   `getrusage` or cgroup accounting on the job runner. Per-session attribution
   needs `/proc` scans. What is the cost budget, given the sampler's "must not
   slow the box" constraint?
8. **Does the planner replace `size_pools` or wrap it?** A new input to the
   existing pure function keeps tests and the fleet-wide sizing semantics of
   the [global worker pools](2026-09-08-global-worker-pools-design.md) design.
   A replacement would reopen them.

## 7. Dependencies and sequencing

- **Hard prerequisite:** [exclusive job queue](2026-09-24-exclusive-job-queue.md).
  Its job classes, duration and resource history, and Phase 0 PSI/lag
  measurement are the planner's inputs.
- **Benefits from** [smart test selection](2026-09-24-smart-test-selection.md).
  Smaller, more uniform test jobs are easier to predict.
- **Interacts with** [agent sleep/wake](2026-09-24-agent-sleep-wake.md). A
  planner that delays a job should let its agent sleep through the delay
  rather than hold a seat.
- **Could feed** [supervisor narrative updates](2026-09-24-supervisor-narrative-updates.md)
  and the [morning report](2026-09-24-morning-report-playbook.md) with ETAs, but
  only once predictions are shown to be accurate.
- **Sequencing:** job queue Phase 0–2, then adaptive job slots (B, jobs
  only), then admission control (A), then a read-only Gantt projection, then
  (maybe) C.

## 8. Non-goals

- No multi-host or cluster scheduling.
- No optimal (ILP/CP-SAT) solver. Greedy list scheduling is the ceiling
  considered here.
- No change to fair-share semantics between projects (`credit_weight`,
  deficit accounting) or to pool placement rules. The planner adds a resource
  constraint and does not re-rank projects.
- No replacement of cgroup hard limits (layer 3). Planning is cooperative,
  and the kernel stays the backstop.
