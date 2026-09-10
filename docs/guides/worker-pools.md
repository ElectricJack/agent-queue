---
tags: [guide, ops, swarm, pools]
---

# Worker pools — the operator's guide

Read [Scheduling, worker pools and resource limits](../concepts/scheduling.md)
first for the distinction between a READY task, fleet-wide desired capacity,
and project-specific placement. This page is the operational procedure for an
operator who has decided to use pull-based workers; it does not make pools the
default lifecycle.

A `lifecycle: task` profile is **pushed** work: the scheduler picks a READY
task, launches a session for it, and that session dies with the task. A
`lifecycle: pool` profile is **pulled**: the daemon keeps a small pool of
long-lived sessions alive, and each one asks for work in a loop with
`aq task claim`.

The difference that matters operationally is *who decides how many workers
exist*. Under push it is the queue, one session per assigned task. Under pull
it is you, through two numbers per profile — `min_active` and `max_active` —
and the daemon converges towards them every 5-second cascade tick. Those two
numbers are **fleet-wide**: one pool per profile, shared by every project
(§3).

This guide is the operational half. The config reference is
[`docs/specs/config.md` §4.11](../specs/config.md); the design is
[swarm-work-model §10–§12, §17](../superpowers/specs/2026-08-28-swarm-work-model-design.md)
as amended by
[global worker pools](../superpowers/specs/2026-09-08-global-worker-pools-design.md)
(which re-scoped pool sizing from per-project to fleet-wide); the end-to-end
test harness is [Swarm E2E testing](e2e-swarm.md).

---

## 1. What has to be true before a pool runs

Three independent switches. All three must agree, and each fails silently in
a different way when it does not.

| Switch | Where | Effect when off |
|---|---|---|
| `swarm.enabled` | `~/.agent-queue/config.yaml` | `_reconcile_pools` is a no-op and `task_claim` answers `not_admissible` / `swarm_disabled`. Pool sessions are never launched. |
| `sessions.enabled` | `~/.agent-queue/config.yaml` | No session of any lifecycle launches. `_reconcile_pools` returns early. |
| `lifecycle: pool` on the profile | profile markdown `## Config` | The profile stays on push assignment. |

The dangerous combination is **`lifecycle: pool` with `swarm.enabled: false`**.
The push path's gates key on `lifecycle` alone and deliberately do *not*
consult the flag, so those tasks are never pushed; the pull path is
flag-gated, so they are never claimed either. They sit in `READY` forever.
`aq doctor` reports exactly this as `pools.disabled` (WARN, report-only —
which switch to flip is an operator decision, not a repair).

```yaml
# ~/.agent-queue/config.yaml
swarm:
  enabled: true
```

Every `swarm:` key is hot-reloadable: the daemon reads `AppConfig.swarm` fresh
each tick and each request, so `aq system update-config` takes effect without a
restart. Turning the flag *on* does not by itself start anything — no profile
is `lifecycle: pool` until you say so.

---

## 2. Turning a profile into a pool

Two commands, and **the order is not optional**: `pool scale` refuses a
profile that is not already `lifecycle: pool`.

```bash
aq pool set-lifecycle --profile-id worker-standard-high-claude --lifecycle pool

aq pool scale --profile-id worker-standard-high-claude --min 0 --max 3
```

Both write the **system** profile:

```
~/.agent-queue/vault/agent-types/<profile>/profile.md
```

Agents are shared between projects, so a profile has exactly one definition
and its lifecycle and bounds are global — the same pool serves every active
project. **Sizing is fleet-wide too**: there is one pool per *profile*, sized
from demand summed across every active project, and `min 0 --max 3` means at
most three workers on this box, not three per project. Which project each
authorised worker is launched into is decided afterwards, by a separate
placement step (§3). A worker is still bound to the project it launched in for
its whole life — its workspace and its token's scope fence are both minted at
launch.

> **Upgrading from per-project pools?** These bounds used to be applied once
> per active project, so the same numbers authorised several times as many
> workers. Read §3a before you decide the new fleet is too small.

Neither command needs `--project-id` any more. It is still accepted for one
release and ignored, with a deprecation warning on the response, so existing
scripts keep working.

The write order inside the command is deliberate: the `agent_profiles` row is
updated **first** so the very next tick honours the new value, then the vault
markdown is written and re-synced. The vault is the source of truth
(swarm-work-model §14), so a DB-only write would be reverted by the next vault
sync. A read-only vault degrades the command to the non-durable DB-only
behaviour with a logged warning rather than failing outright — worth knowing,
because the change will then silently disappear on the next sync.

Both commands are admin surfaces. A worker session's task-scoped token gets
`out of scope: pool_scale`.

### From the dashboard

The Agents page's create control opens a fork — **Create agent** or **Create
agent pool** — because the two are different objects: one durable global worker
versus per-project elastic capacity. The pool form covers the second command
only: it lists the profiles that already carry `lifecycle: pool`, takes the
project whose pool view to open, collects `min_active` / `max_active`, and calls
`pool_scale`. Giving a profile pool lifecycle in the first place is still
`aq pool set-lifecycle`, and the form says so when no profile is eligible.

The dashboard still addresses pools per `(project, profile)`: the project it
asks for is the pool view it opens, not a pool of its own, and the bounds it
writes are the profile's fleet-wide ones (`pool_scale` ignores `project_id`).
The CLI and API are the accurate surfaces until the Agents page is re-keyed.

The create-agent form deliberately cannot build a durable worker on a pool
profile: the profile is shown but disabled, and a submit is refused, because
such a worker becomes a pool instance the flock files under its pool entry —
which reads as a creation that silently failed.

### The pool-only config keys

These live in the (system) profile's own `## Config` block, not in
`config.yaml`, and the parser rejects them on a `task` or `named` profile:

| Key | Meaning |
|---|---|
| `min_active` | Fleet-wide floor. `0` means "no idle workers when the queue is empty". |
| `max_active` | Fleet-wide ceiling for this profile, across every project. `null` = no profile limit (`swarm.global_max_active` and each project's cap still apply). |
| `min_per_project` | Per-project warm floor: keep this many workers resident in *every* eligible project. Defaults to `0`. It reserves fleet slots — see below. |
| `max_claims_per_session` | How many tasks one session may claim before it retires. Ignored while `swarm.fresh_context_per_task` is true, which pins the effective cap at 1. |
| `enabled` | Operator kill-switch. `false` hands the profile no new work. Defaults to `true`; written by `aq pool set-enabled`. |

### `min_per_project` — buying back a warm worker

Fleet-wide bounds mean `min_active` no longer parks a resident worker in every
project: `min_active: 2` keeps two workers alive *somewhere*, and placement
puts them where the demand is. A project that is quiet when the floor is
placed gets no resident worker, and the first task that arrives there pays a
cold launch — workspace acquisition, harness boot, prime.

`min_per_project` is how you buy that back where it actually matters, and only
there:

```markdown
## Config
lifecycle: pool
min_active: 4
max_active: 12
min_per_project: 1
```

- The reservation is honoured **first**: placement gives a start to a project
  below its warm floor before it looks at demand anywhere else, and a drain
  never takes a project at or below its floor.
- It **raises the effective floor** rather than being ignored when
  `min_active` cannot fund it: the floor sizing uses is
  `max(min_active, Σ min_per_project over eligible projects)`. With
  `min_per_project: 1` and six eligible projects, `min_active: 4` becomes an
  effective floor of 6.
- Only *eligible* projects count towards that sum. A quarantined project, or
  one with no free workspace, does not hold a reservation open against the
  rest of the fleet.
- **It costs fleet slots.** Reserved warm workers are inside `max_active`, not
  on top of it, so every `min_per_project` worker is one fewer worker
  available to burst into the busiest project. If the effective floor exceeds
  `max_active` the configuration contradicts itself: sizing clamps to
  `max_active`, some projects silently never get their warm worker, and
  `aq doctor --check pools.floor_exceeds_max` says so.

There is no `aq pool` flag for it — `aq pool scale` writes `min_active` and
`max_active` only. Set `min_per_project` in the system profile's `## Config`
block in the vault; the vault watcher syncs it to the `agent_profiles` row and
the next tick honours it. `aq pool set-lifecycle --lifecycle task` clears it
along with the other pool-only keys.

### Switching a pool off without deleting it

When a tier runs low on remaining usage — a fable-level pool, say — turn it
off rather than unpicking its definition:

```bash
aq pool set-enabled --profile-id worker-fast-medium-claude --no-enabled
aq pool set-enabled --profile-id worker-fast-medium-claude --enabled
```

The switch lives in the system profile's `## Config` (so it survives the next
vault sync) and, like every other pool edit, is global: every project's pool
for that profile is affected. What it changes is eligibility for **new** work:

* the sizer reads bounds `(0, 0)` for the profile, so idle workers drain on the
  usual `scale_down_grace` schedule and nothing new is started;
* `desired` is still floored at `running_busy + starting`, so a worker holding
  a task keeps its session and finishes that task;
* that worker's next `aq task claim` answers `drain_requested`, so it stops
  rather than taking another task — including a claim already long-polling.

The pool keeps its `pool status` row (with `enabled: false`) and its place in
the dashboard's pool directory, which is where the same switch lives as a
toggle on each row; non-pooled agents carry the equivalent toggle on their
flock row, backed by `agents.enabled` and `aq agent edit --agent-id <id> --no-enabled`.

---

## 3. How the size is chosen

Two steps per cascade tick, and they answer different questions. **Sizing**
(`src/scheduler.py:size_pools`) decides *how many* workers a profile should
have, fleet-wide, and no longer knows that projects exist. **Placement**
(`place_pool_actions`, same file) decides *which project* each authorised
start goes to, and which project a drain comes from. Both are pure — no I/O,
no clock — and run against a single measurement pass (`_measure_pools`).

### 3.1 Sizing — fleet-wide

```
want    = running_busy + ready
desired = clamp(want, min_active, max_active)          # max_active None = unbounded
desired = max(desired, running_busy + starting)        # never undercut work in flight
```

Every quantity is a sum across every active project: `running_busy` is the
whole fleet's busy workers, and `ready` is the whole fleet's demand for that
profile — READY tasks routed to it, plus a project's *unrouted* READY tasks
where this profile is that project's default.

`min_active` is raised to `max(min_active, Σ min_per_project over eligible
projects)` before the clamp, so a per-project warm floor the global floor
cannot fund raises the floor instead of being silently ignored.

Two ceilings bind the sizer, in order:

1. **`swarm.global_max_active`** — the box-wide ceiling on live pool workers
   across *every* profile. Unset (the default) it resolves to
   `resources.max_concurrent_agents` (default 8), so the bound always exists;
   set it explicitly to size the fleet independently of the resource knob
   (that same `resources.max_concurrent_agents` also divides the per-session
   test-worker share, which is why pool size gets its own key).
2. **`swarm.max_starts_per_tick`** (default 2) — starts are handed out one at
   a time, round-robin across every pool that still wants more, so a saturated
   cap shares the remaining headroom instead of starving whichever pool sorts
   last. This is now 2 per profile *in total* rather than 2 per profile per
   project, so a 0→8 ramp takes 4 ticks (~20 s) rather than being parallelised
   across projects.

`projects.max_concurrent_agents` is **not** a sizing input any more. It is a
property of a project, not of a pool, so it moved to placement.

**Scale-down** is deliberately reluctant. It only ever drains *idle* sessions,
only after the pool has been continuously in surplus for
`swarm.scale_down_grace` seconds (default 120), and at most
`swarm.max_drains_per_tick` (default 5) per tick. A drain sets
`desired_state='stopped'`; the session finishes what it is doing and its next
claim attempt returns `drain_requested`. A busy worker is never drained
mid-task.

`aq pool scale --now` is the exception: it terminates idle sessions above the
new effective max immediately, oldest first, skipping the grace window.

### 3.2 Placement — which project gets the worker

A worker is bound to one project for its whole life (its workspace and its
token's scope fence are minted at launch), so this is the only moment the
choice is made.

A project may receive a start only if **all** of these hold — the first one it
fails is the blocking reason reported on a starvation:

| Predicate | Blocking reason |
|---|---|
| the `(project, profile)` key is not quarantined (§4a) | `quarantined` |
| the project has a free `project-repo` workspace | `no workspace capacity` |
| the project is under its own `projects.max_concurrent_agents`, counting all its pool sessions of any profile | `at project cap (N)` |
| the project is not already holding an idle worker with nothing queued behind it | `an idle worker is already available` |

Eligible projects are then ordered, re-sorting after every single start so a
budget of two does not pile onto whichever project sorts first:

1. projects below their `min_per_project` warm floor, largest shortfall first;
2. then by unserved deficit `ready - (idle + starting)`, largest first;
3. then by fewest live workers — spread rather than concentrate;
4. then `project_id` ascending, so the outcome is deterministic.

The winner's reason (`warm_floor` / `deficit` / `spread`) is carried on the
`pool.scaled` event as `placement_reason`, which is the answer to "why *there*?".

**Drains invert it.** A project at or below its warm floor is excluded
outright; the rest are ordered by idle surplus relative to their own demand
(`idle - max(0, ready - starting)`), largest first, and the oldest idle
session in the chosen project goes. That is what stops a quiet project's one
warm worker from being reaped first simply for being idle and old.

When global supply already matches demand but idle workers are in another
project, the pool waits the scale-down grace period and drains only idle
surplus above that project's warm floor. A destination must have unserved
ready work, workspace capacity and room under its project cap. The next
normal sizing pass replaces the retired capacity; rebalancing never grants
extra starts or raises the configured limits.

**Nowhere to put it.** When the sizer authorises a start and no project is
eligible, the start is not silently dropped: it comes back as a starvation,
logged once per condition (not per tick), persisted as
`pool.placement_starved` with the blocking reason per project, and reported by
`aq doctor --check pools.placement_starved` once it has held for five minutes.

Over-subscribing per-project caps is still the intended arrangement: six
profiles capped at 3+2+2+2+1+1 = 11 against a project cap of 8 does not mean 11
workers in that project, it means whichever pools want capacity fair-share
the 8.

### Reading the shape

```bash
aq pool status                          # every pool, fleet-wide
aq pool status --project-id agent-queue # view filter on the breakdown
aq session list --lifecycle pool --live
```

`pool status` gives **one row per profile**. `min_active`, `max_active`,
`min_per_project` (the `Min/pp` column), the computed `desired`, the supply
split (`running_idle` / `running_busy` / `starting` / `draining`) and the
`ready` demand are all fleet-wide numbers; the trailing **Projects** column is
the placement summary — one compact token per project,
`idle/busy/starting/draining` with zero parts omitted:

```
                                Worker pools
 Profile                      Min Max Min/pp Desired Idle Busy Start Drain Ready Projects
 worker-standard-high-claude    0   3      0       3    1    2     0     0     4 agent-queue:1i/1b  api:1b
 worker-standard-low-codex      0   1      1       1    1    0     0     0     0 agent-queue:1i

Quarantined
  worker-standard-low-codex in api — quarantined until 14:02:11 — harness 'codex' is not registered
```

Quarantine is a property of a `(project, profile)` pair, never of the pool, so
it is no longer a column: it renders as a block under the table, where the
captured startup output has room to be readable.

`--project-id` is a **view filter** on the per-project breakdown and the
instance list, never pool identity: the bounds and the aggregate counters stay
fleet-wide whether or not you pass one, because filtering them would misreport
the pool the sizer actually acts on. A profile with no standing in the named
project drops out of the listing entirely.

Over the API the shape matches: `PoolStatusRow` carries `projects[]`
(`PoolProjectStatus`: `ready`, the four counters, `max_concurrent_agents`,
`workspace_capacity`, `quarantined_until` / `quarantined_reason`) and
`instances[]`, each instance naming the `project_id` it launched into.

## 3a. Upgrading from per-project pools

Bounds used to be copied into one pool key per active project, so `max_active`
was quietly multiplied by the size of your project roster. The arithmetic on a
five-project install:

| Setting | What you read | What you got before | What you get now |
|---|---|---|---|
| `min_active: 2` | 2 resident workers | 10 | 2 |
| `max_active: 4` | at most 4 workers | at most 20 | at most 4 |

**Bounds are deliberately not auto-multiplied on upgrade.** Multiplying them
would preserve the exact bug the re-scoping removes, and a 20-worker fleet is
not something to restore silently on a box that may not have 20 workers' worth
of RAM. So the fleet shrinks, and says so:

- One `pool.bounds_rescoped` audit event per pool profile on the first
  reconcile tick after the upgrade (and once per daemon lifetime after that),
  carrying `eligible_projects`, the new `effective_max_active` and the
  pre-upgrade `previous_effective_max_active`:

  ```bash
  aq system get-recent-events --event-type pool.bounds_rescoped --since 24h
  ```

- `aq doctor --check pools.global_bounds_migration` (INFO, report-only) reads
  the oldest of those events per profile and states the loss, naming the
  `max_active` that would buy the old capacity back.

Decide the fleet size you actually want and set it:

```bash
aq pool scale --profile-id worker-standard-high-claude --max 12
```

There is no `--fix`: choosing a fleet size is an operator decision. The notice
resolves once `max_active` differs from the ceiling recorded at the upgrade —
so if you are keeping the smaller fleet on purpose, re-affirming the *same*
number does not silence it; scale to whatever you mean, once, and it is gone
for good.

Sanity-check the result against the box rather than against the old numbers:
`swarm.global_max_active` (default `resources.max_concurrent_agents`, 8) caps
the whole fleet regardless of what the profiles say, so a `max_active` of 20
across three profiles buys nothing on a box whose global cap is 8.

## 3b. What fleet-wide sizing costs

Recorded here so they are not rediscovered as bugs. They are consequences of
the design, not defects:

- **No per-project warm floor by default.** `min_active` parks workers
  *somewhere*, not everywhere. A project that is quiet when the floor is
  placed pays a cold launch for its next task. `min_per_project` buys it back
  where you want it, at the cost of fleet slots (§2).
- **Warmth follows demand.** Deficit-ordered placement concentrates workers in
  the busiest project. Above `min_per_project`, nothing keeps a worker warm in
  a project with no work — nor should it.
- **Cross-project isolation is weaker.** A 500-task backlog in project A used
  to be unable to touch project B's pool, because B had one of its own. Under
  one fleet, A absorbs capacity up to `max_active`.
  `projects.max_concurrent_agents` still ceilings A's share and
  `min_per_project` still floors B's, but there is no proportional fair-share
  between projects — a project that needs a guaranteed share needs its own
  profile.
- **Slower cold ramp under burst.** `max_starts_per_tick` is 2 per profile in
  total now (§3.1), so a ramp is a few ticks rather than N projects' worth of
  starts in parallel.
- **Workers stay project-bound for their lifetime.** Placement chooses once,
  at launch; a worker never switches repos. With
  `swarm.fresh_context_per_task` on (the default) that binding only has to
  last one task.

### Reading the history

`pool status` shows the current shape, never the fact that it changed. Four
pool events are written to the event table; `pool.scaled` is the answer to
"why did a worker appear at 03:14?" and its `placement_reason`
(`warm_floor` / `deficit` / `spread`) is the answer to "why *there*?":

```bash
aq system get-recent-events --event-type 'pool.*' --since 1h
aq system get-recent-events --event-type pool.scaled --project-id agent-queue
```

| Event | Emitted when | Persisted |
|---|---|---|
| `pool.scaled` | the sizer started or drained sessions (carries the project placement chose and its `placement_reason`) | yes |
| `pool.placement_starved` | starts were authorised and no project could take them; carries the blocking reason per project. Written when the condition appears or its reasons change, never per tick | yes |
| `pool.bounds_rescoped` | first reconcile tick of a daemon lifetime, once per pool profile: what the ceiling was under per-project bounds and what it is now (§3a) | yes |
| `pool.session_started` | a launch succeeded | bus + WebSocket only |
| `pool.session_claimed` | a session took a task | bus + WebSocket only |
| `pool.session_drained` | a session was torn down (carries the reason) | bus + WebSocket only |
| `pool.session_quarantined` | a session went terminal-bad | bus + WebSocket only |
| `pool.bounds_changed` | `pool scale` | bus + WebSocket only |
| `pool.lifecycle_changed` | `pool set-lifecycle` | bus + WebSocket only |
| `pool.enabled_changed` | `pool set-enabled` | bus + WebSocket only |
| `pool.agent_repaired` | `aq doctor --fix` touched an agent row | yes |

---

## 4. Quarantine — two different things with one name

`pool status` can show the word twice, and the two mean different things.
**Key quarantine** stops the daemon *launching* into a pool. **Session
quarantine** takes one already-running worker out of service.

### 4a. Key quarantine — why a pool stops growing

A launch failure that will repeat identically next tick quarantines the
`(project_id, profile_id)` key for **60 seconds** (`pools.LAUNCH_BACKOFF`),
so a broken pool creates and deletes one agent row a minute instead of one
every 5-second tick. `aq pool status` surfaces `quarantined_until` **and**
`quarantined_reason` in its per-project breakdown (rendered as the
*Quarantined* block under the table) — a bare deadline left operators staring
at a stalled pool with nothing to act on.

The key stays `(project_id, profile_id)` even though the pool itself is now
global: a quarantine is always project-specific — an unknown harness for that
checkout, a startup death in that repo — and collapsing it to the profile
would let one broken project disable a healthy fleet. It is consulted as a
*placement* predicate, **before** a start is spent, so a broken project cannot
burn the fleet's `max_starts_per_tick` budget every tick; the fleet simply
places those workers somewhere else, and only reports a starvation if there is
nowhere else.

Quarantining failures:

- advertised capacity cannot be acquired (including a missing `project-repo`
  kind or worktree provisioning that produced no usable slot);
- the configured session provider cannot be constructed;
- the profile's `harness` is unknown to the registry;
- the workspace resolved to the **base checkout** (a pool session may not run
  there — see `src.orchestrator.base_workspace`);
- the harness process died during startup — the reason carries the last ~400
  characters of the captured startup output, read once and then reused, so one
  dead harness does not become a wall of identical stack traces;
- the session started but its `sessions` row could not be written;
- any other exception out of acquisition, token mint, spec build or launch.

Pool capacity counts only the `project-repo` kind that workers actually acquire;
the auto-attached vault is not an execution slot. A disabled base repository
prevents lazy worktree growth. Check the base's enabled state as well as its
slots when a project has ready work but no capacity. Placement shares workspace
and project concurrency budgets across all profiles in a tick. When unserved
demand is blocked in one project, it does not launch workers into empty projects
to satisfy that demand; explicit per-project warm floors still apply.

Pool launches run in the background. A slow worktree operation or harness
startup does not hold up other projects, drains, session reconciliation, or
message delivery. Pending launches count as starting supply and reserve the
profile, project, fleet, and workspace capacity they need. Once a launch locks
its workspace, that lock replaces its workspace reservation so another free
slot remains usable. Live launch identities are protected from orphan recovery
even when preparation exceeds two minutes. Within each project and workspace
kind, slot growth and acquisition finish together before another launch requests
a slot, preventing two workers from provisioning the same next slot. Shutdown cancels unfinished launches
and confirms their processes have stopped before releasing resources; sessions
that already have durable rows remain owned by normal session reconciliation.

Pool workers keep their claim loop alive during daemon restarts. `aq task claim
--next --wait 60` retries connection failures within its wait window; if the
daemon remains unavailable, it exits with code 3 and the worker retries the
claim loop. AQ sessions never receive an interactive offer to start the daemon.
An ambiguous response failure is not automatically replayed: after a lost close
response, inspect the current claim and task status before attempting another close.

Zero measured workspace capacity prevents placement without a backoff. If
capacity was advertised but acquisition fails, the key backs off for 60 seconds
so another project can use the next launch opportunity. Disabled worktree slots
occupy their indices but provide no capacity; they never count as free slots or
prevent growth into an unused index below the project cap. Preserved slots stay
disabled until explicitly recovered. Inspect the inventory with:

```bash
aq project list-workspaces --project-id agent-queue   # who holds what
aq doctor --check worktrees.orphans --check workspaces.base_sessions
```

Every failure path rolls back completely: the workspace lock is released, the
agent row goes back to `IDLE`, and a minted session token is revoked. There is
no half-launched state to clean up by hand.

### 4b. Session quarantine — one worker taken out of service

Separately, `SessionReconciler` marks an individual session row
`state='quarantined'` — terminal, never restarted — after it burns through
`sessions.max_restarts`. Three reasons reach it:

| `end_reason` | Trigger |
|---|---|
| `rapid_crash` | the harness kept dying immediately after launch |
| `stall` | no activity past the lease, through the full nudge/interrupt/restart ladder |
| `start_failed` | the session never came up at all |

The blast radius is wider than a drain: the held task is transitioned to
`BLOCKED` with task-meta `needs_attention: session_<reason>`, and
`pool.session_quarantined` is emitted alongside `session.quarantined`. That
task will not move again until a human clears it — quarantine is the daemon
saying it has stopped guessing.

A quarantined row is excluded from pool supply (only `starting`, `running`
and `draining` count), so the pool simply launches a replacement on the next
tick. `aq pool status` shows the dead instance with its `quarantine_reason`
until the row is swept.

```bash
aq session list --state quarantined
aq system get-recent-events --event-type pool.session_quarantined --since 1h
```

---

## 5. Doctor checks

```bash
aq doctor                                   # everything
aq doctor --check pools.orphan_agents       # one check
aq doctor --fix                             # apply fixable repairs, then re-run
```

| Check | Severity | Fixable | What it means |
|---|---|---|---|
| `pools.stuck` | ERROR | yes | A running pool session still holds a `task_id` whose task is no longer IN_PROGRESS/ASSIGNED. |
| `pools.orphan_agents` | WARN / ERROR | yes | A pool-profile `agents` row with no session row at all, older than `2 × prepare_timeout`. |
| `pools.preparing_stuck` | ERROR | yes | A session sat in `claim_phase` `claiming`/`preparing` past `2 × prepare_timeout` with no live preparation request. The fix releases the exact expired preparation as `prepare_failed`; an active reset or a claim that has since activated stays held. |
| `pools.stranded_feature_branches` | WARN / INFO | prints the command | A remote branch that has had PRs merged **into** it, is ahead of the default branch, and has no open PR taking it there — its merged work is not on `main`. A second, INFO-only bucket lists non-`aq/` branches ahead of the default branch with no open PR to it. `--fix` prints the `gh pr create --base <default> --head <branch>` command; it never opens the PR, because `aq doctor --fix` runs every fix and opening a PR is outward-facing and not undoable. |
| `pools.disabled` | WARN | no | Pool profiles exist but `swarm.enabled` is false — see §1. Report-only on purpose. |
| `pools.global_bounds_migration` | INFO | no | This profile's ceiling shrank when bounds became fleet-wide, and nobody has re-scaled it since. Names the old effective ceiling, the new one, and the `max_active` that would restore it — §3a. Report-only: fleet size is an operator decision. |
| `pools.floor_exceeds_max` | WARN | no | `max(min_active, Σ min_per_project)` is greater than `max_active`. Nothing breaks — sizing clamps to `max_active` — but some projects will never get the warm worker their `min_per_project` asks for. Raise `max_active` or lower `min_per_project`. |
| `pools.placement_starved` | WARN | no | A profile has had authorised starts and no eligible project for over 5 minutes, with the blocking reason per project (`quarantined` / `no workspace capacity` / `at project cap`). Reads the running orchestrator's observation, so it reports INFO ("cannot see") when no daemon is reachable. |
| `claims.holder_consistency` | WARN | no | An IN_PROGRESS task whose claim holder disagrees with `agents.current_task_id` or with the `claimed_by_session` task-meta. Report-only. |

### The agent-row rule, and the one thing not to do

`_launch_pool_session` creates one `agents` row per pool session and
`_terminate_pool_session` gives it back — but *which state* it comes back in
is the whole rule:

- **Confirmed stop → `IDLE`.** The definition returns to the reuse pool, and
  the next launch draws its candidate from `list_agents(state=IDLE)`. That
  reuse is what bounds the roster at roughly `max_active` per pool instead of
  growing it by one row per claimed task.
- **Unconfirmed stop → `RETIRED`.** The row is marked `RETIRED` *before*
  `provider.stop` and cleared back to `IDLE` only once the stop is confirmed,
  so a worker whose process might still be alive is never handed to a second
  session.

`pools.orphan_agents` sorts what falls outside that loop into four buckets:

| Shape | Verdict |
|---|---|
| idle, enabled, unowned, no workspace | the reuse pool — left alone, reported as `spares` |
| idle but still holding a workspace lock | a rolled-back launch leaked it; `--fix` releases the lock and leaves the row `IDLE` |
| busy, or `current_task_id` set | reported, never touched — it may still own a task, and retiring it would strand that task |
| disabled, or `ERROR`/`PAUSED` | unusable and unowned; `--fix` retires it (never deletes) |

Every repair writes a `pool.agent_repaired` event, so
`aq system get-recent-events --event-type pool.agent_repaired` still answers
"why is this worker RETIRED?" long after the doctor run has scrolled away.

Deleting a worker removes that identity from the usable roster while preserving
its history. It does not disable automatic pool growth: when queued demand and
pool bounds allow it, AQ can create a new worker identity. Existing compatible
idle workers are reused first. To reduce capacity persistently, change the pool's
`max_active` bound or disable the pool; deleting individual workers is not a
scaling policy. Live-session and ownership checks still prevent unsafe deletion.

---

## 6. Rolling back

The rollback is one command per profile and needs no restart:

```bash
aq pool set-lifecycle --profile-id worker-standard-high-claude --lifecycle task
```

What it does, in order:

1. Writes `lifecycle: task` into the system profile *and* clears `min_active`,
   `max_active`, `min_per_project` and `max_claims_per_session` — the parser
   rejects those keys on
   a task profile, so leaving them behind would make the profile fail its next
   vault sync.
2. Marks every live pool session for that profile `desired_state='stopped'`,
   **in every project**. They are **not** killed: a session holding a task keeps
   it and releases it through the normal close path; the next claim attempt
   returns `drain_requested`.
3. Emits `pool.lifecycle_changed` and one `pool.session_drained` per session.

From the next tick the push scheduler assigns that profile's tasks again.

The lifecycle is global, so rollback is fleet-wide: there is no way to keep a
profile on pull in one project and push in another. If one project needs a
different execution model, give it a different profile.

To roll back everything at once, flip the master switch instead:

```bash
aq system update-config --section swarm --data '{"enabled": false}'
```

That stops `_reconcile_pools` and makes new claims inadmissible immediately —
but it leaves the profiles on `lifecycle: pool`, which is precisely the
stranded state from §1. It is the right lever for an emergency stop and the
wrong one for a durable rollback; follow it with per-profile
`set-lifecycle task`, and expect `aq doctor` to report `pools.disabled` until
you do.

Watch it settle:

```bash
aq pool status --project-id agent-queue
aq session list --lifecycle pool --live
aq system get-recent-events --event-type 'pool.*' --since 10m
```

---

## 7. A cutover runbook

For a fleet moving several worker profiles at once. Every step is an admin
surface; a task-scoped worker token cannot run any of them.

Lifecycle and bounds are configured on the system profile and apply to the
whole fleet: one pool per profile, sized against demand from every project
(§3). The `--project-id` in the *inspection* commands below is a view filter
on the per-project breakdown, not a pool address.

**1. Check the preconditions.**

```bash
aq doctor --check pools.disabled --check pools.orphan_agents
aq system get-config --section swarm            # note global_max_active
aq project get --project-id agent-queue         # note max_concurrent_agents
```

Size the fleet against the box, not against the project roster: the sum of
every profile's `max_active` is bounded by `swarm.global_max_active`, which
defaults to `resources.max_concurrent_agents` (8).

**2. Flip lifecycle, then bounds, one profile at a time.** Do the smallest,
least-loaded profile first and watch a full tick before continuing.

```bash
P=agent-queue
aq pool set-lifecycle --profile-id worker-standard-low-claude --lifecycle pool
aq pool scale         --profile-id worker-standard-low-claude --min 0 --max 1
aq pool status --project-id $P
```

Keep `min 0` for every pool unless you are deliberately paying for a warm
worker: a floor above zero holds sessions open against an empty queue, and
under fleet-wide bounds it holds it open against *every* project's queue at
once. `min_per_project` is the way to say "warm, and in this project" (§2).

Sum the maxima against the global cap and each project's cap knowingly.
Over-subscription is a fair-share arrangement, not a promise — with a cap of 8
and maxima summing to 11, three profiles' worth of demand is what gets
squeezed, and which three depends on arrival order.

**3. Confirm each pool actually claims.** A pool that launches but never
claims is the failure mode worth catching early:

```bash
aq pool status --project-id $P                       # running_busy > 0
aq system get-recent-events --event-type pool.session_claimed --since 10m
aq doctor --check claims.holder_consistency --check pools.preparing_stuck
```

Verify at least one pool per **harness** (`claude` and `codex` behave
differently at startup), and check that fan-out siblings run concurrently
rather than serialising — concurrency here is bounded by the project cap and
by available workspaces, not by the pool maxima alone.

**4. Leave the old fixed agent rows alone.** They are `IDLE` worker
definitions, which is exactly what `_launch_pool_session` prefers to reuse.
If a row must be taken out of service, disable it
(`aq agent edit --agent-id <id> --no-enabled`) — do not delete it; see the
warning in §5.

**5. Record it.** `pool.bounds_changed` and `pool.lifecycle_changed` are
bus-only events and are gone once the daemon restarts. The durable record of a
cutover is the system profile markdown in the vault and whatever you write
down:

```bash
git -C ~/.agent-queue/vault status --short   # if the vault is version-controlled
```

### Worked example — this repo's own cutover

The bounds agreed for a single-project fleet (`agent-queue`), under a project
cap of 8 and the default global cap of 8. On a multi-project box these are the
numbers for the *whole* fleet, not per project — see §3a before reusing them.
`triage`, `supervisor` and `worker-deep-high-claude` stay on `lifecycle: task`.

| Profile | Harness | `min` | `max` |
|---|---|---|---|
| `worker-standard-high-claude` | claude | 0 | 3 |
| `worker-standard-medium-claude` | claude | 0 | 2 |
| `worker-standard-low-claude` | claude | 0 | 1 |
| `worker-standard-high-codex` | codex | 0 | 2 |
| `worker-deep-medium-codex` | codex | 0 | 2 |
| `worker-standard-low-codex` | codex | 0 | 1 |

The maxima sum to 11 against a cap of 8 — deliberate over-subscription, so a
quiet profile's headroom is usable by a busy one (§3).

```bash
P=agent-queue
for spec in worker-standard-low-claude:1  worker-standard-low-codex:1 \
            worker-standard-medium-claude:2 worker-deep-medium-codex:2 \
            worker-standard-high-codex:2   worker-standard-high-claude:3; do
  profile=${spec%:*}; max=${spec#*:}
  aq pool set-lifecycle --profile-id "$profile" --lifecycle pool
  aq pool scale         --profile-id "$profile" --min 0 --max "$max"
done
aq pool status --project-id $P
```

Ordered smallest-first on purpose: the first two lines are the cheap probe
that the vault write, the harness and the claim loop all work, before the
profiles that carry the real load are moved.

### Upgrading a vault that still has project overrides

Before profiles became global, these bounds lived in
`vault/projects/<pid>/agent-types/<profile>/profile.md`. Those overrides no
longer resolve. The daemon promotes each one into its system profile on the
next startup — last writer wins on `## Config`, with the per-key diff logged —
and you can run the same migration by hand:

```bash
aq doctor --check profiles.project_overrides            # what is left
aq doctor --check profiles.project_overrides --fix      # promote and delete
```

Prose sections an override added (a custom `## Role`, say) are **not** merged;
the check names them so you can hand-merge before or after. Two projects that
overrode the same profile differently collapse into one definition — check the
result with `aq agent get-profile <id>` and re-scale if the surviving numbers
are not the ones you want.

---

## 7b. Migrating a vault seeded before the provider-explicit rename

Shipped worker profiles used to have provider-implicit ids — `worker-fast`,
`worker-standard`, `worker-deep`, all of them silently on the `claude`
harness. They now state their provider, and their level, in the id:

| Old id | New id | Display name |
|---|---|---|
| `worker-fast` | `worker-fast-medium-claude` | Claude · Fast (Medium) |
| `worker-standard` | `worker-standard-medium-claude` | Claude · Standard (Medium) |
| `worker-deep` | `worker-deep-high-claude` | Claude · Deep (High) |

The convention is `worker-<tier>-<level>-<provider>`, so a Codex sibling is a
separate profile (`worker-standard-medium-codex`) rather than the same profile
with its harness repointed — the id always describes what actually runs.

**Nothing renames itself.** An existing vault keeps its old directories, and
startup seeding is write-if-absent, so an upgrade adds the three new ids
alongside the three old ones. Migrate deliberately:

**1. See what you have.**

```bash
ls ~/.agent-queue/vault/agent-types/
aq agent profile-drift            # `retired` / `not_seeded` rows included
```

**2. Move open work off the old ids.** Tasks and project defaults still point
at the old profile, and deleting it clears those references.

```bash
aq task list --project agent-queue --status READY   # find the ones still pinned
aq task route --task-id <task-id> --profile-id worker-standard-medium-claude
aq project set agent-queue default-profile worker-standard-medium-claude
```

**3. Move pool bounds across.** Bounds live on the profile itself, so they do
not follow a rename — set them on the new id and stand the old one down:

```bash
aq pool set-lifecycle --profile-id worker-standard-medium-claude --lifecycle pool
aq pool scale         --profile-id worker-standard-medium-claude --min 0 --max 2
aq pool set-lifecycle --profile-id worker-standard --lifecycle task
```

**4. Delete the old profile.**

```bash
aq agent delete-profile --profile-id worker-standard \
    --reason "moved to the provider-explicit ladder"
```

Nothing re-creates it: `worker-standard` is no longer a shipped id, so startup
seeding has nothing to seed it from.

### Retiring a shipped default

Deleting one of the *current* shipped defaults is the case that used to
regress. `ensure_default_profiles()` runs on every daemon start and is
write-if-absent, so "no `vault/agent-types/<id>/`" read as *fresh install* and
the profile came straight back — along with any pool that had been sized
against it.

`aq agent delete-profile` now records a shipped id in
`~/.agent-queue/vault/agent-types/.retired-defaults`, and seeding skips every
id listed there:

```bash
aq agent delete-profile --profile-id worker-fast-medium-claude \
    --reason "fleet runs the -high variants only"
```

The response carries `retired: true`, and `aq agent profile-drift` reports the
profile as `retired` rather than `not_seeded` — the two need opposite advice,
so they are separate statuses.

To bring one back, reseed it; that clears the tombstone in the same command
and syncs the restored profile to the database without waiting for the vault
watcher:

```bash
aq agent profile-reseed --profile-id worker-fast-medium-claude
```

The tombstone is a small JSON file (`{"version": 1, "retired": {...}}`) that
records when each id was retired and why, so a hand edit works too — but the
reseed command is the supported path.

> Only `aq agent delete-profile` writes a tombstone. Removing
> `vault/agent-types/<id>/` with `rm -rf` leaves no record, and the next
> restart re-seeds it.

---

## 8. Symptom → cause

| Symptom | Look at |
|---|---|
| Tasks sit in READY, nothing launches | `aq doctor --check pools.disabled`; `swarm.enabled` and `sessions.enabled` |
| `pool status` shows a pool flat at 0, with `quarantined_reason` | §4a — harness, provider, base checkout, or a dead startup |
| `pool status` shows a pool flat at 0, no quarantine reason | starved: no `project-repo` kind, or no free workspace. `aq doctor --check pools.placement_starved` names the blocking reason per project |
| `desired` is below `ready` | `max_active` or `swarm.global_max_active` is binding — not a bug |
| The fleet is much smaller than it was before an upgrade | bounds are fleet-wide now: `aq doctor --check pools.global_bounds_migration` (§3a) |
| A project never gets a warm worker | placement follows demand; set `min_per_project` (§2) — and check `pools.floor_exceeds_max` if you already did |
| `desired` is met but one project has no worker | placement chose elsewhere: read the `Projects` column and `pool.scaled`'s `placement_reason` |
| Workers appear and vanish every minute | a quarantining launch failure; read `quarantined_reason` (§4a) |
| A session holds a task that is already closed | `aq doctor --check pools.stuck --fix` |
| A completed integration task still has a running pool claim and attached branch owner after a restart | As a local operator, run `aq integration flush <project-id>`; it recovers only a quiescent, clean, origin-published exact holder. |
| A claim never completes | `aq doctor --check pools.preparing_stuck --fix` (releases as `prepare_failed`) |
| Pools stopped growing and nothing is quarantined | a soft-deleted worker row is fencing `create_automatic_agent` — §5 |
| A task is BLOCKED with `needs_attention: session_stall` | the session was quarantined (§4b); clear the task by hand |
| The pool never scales down | `scale_down_grace` has not elapsed, or every session is busy |

For the terminal-close recovery above, use the normal public integration
surface; do not force the task READY or manually clear the session/workspace
rows:

```bash
aq task get --task-id keen-harbor.8
aq integration flush agent-queue
aq task get --task-id keen-harbor.8
aq pool status --project-id agent-queue
```

The flush preserves the task's COMPLETED status, completion/review evidence,
and published head. It refuses a live or unprobeable writer, a dirty checkout,
an unpublished head, a stale claim epoch, or a session/workspace that has been
reused; those cases remain fenced for investigation rather than being unlocked.

---

## See also

- [`docs/specs/config.md` §4.11](../specs/config.md) — every `swarm:` key,
  its default and its validation.
- [Swarm E2E testing](e2e-swarm.md) — a real daemon on real PostgreSQL through
  the whole claim protocol in ~2½ minutes, no LLM.
- [Resource gating](resource-gating.md) — the box-wide caps that decide how
  much CPU the pool's sessions may actually use.
- [swarm-work-model design](../superpowers/specs/2026-08-28-swarm-work-model-design.md)
  §10–§12, §17 — the original pool design; its §11 keys pools per
  `(project, profile)` and is superseded on that point.
- [global worker pools design](../superpowers/specs/2026-09-08-global-worker-pools-design.md)
  — fleet-wide sizing, the placement step, `min_per_project` and
  `swarm.global_max_active`.
