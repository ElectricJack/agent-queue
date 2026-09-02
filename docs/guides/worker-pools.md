---
tags: [guide, ops, swarm, pools]
---

# Worker pools — the operator's guide

A `lifecycle: task` profile is **pushed** work: the scheduler picks a READY
task, launches a session for it, and that session dies with the task. A
`lifecycle: pool` profile is **pulled**: the daemon keeps a small pool of
long-lived sessions alive, and each one asks for work in a loop with
`aq task claim`.

The difference that matters operationally is *who decides how many workers
exist*. Under push it is the queue, one session per assigned task. Under pull
it is you, through two numbers per profile — `min_active` and `max_active` —
and the daemon converges towards them every 5-second cascade tick.

This guide is the operational half. The config reference is
[`docs/specs/config.md` §4.11](../specs/config.md); the design is
[swarm-work-model §10–§12, §17](../superpowers/specs/2026-08-28-swarm-work-model-design.md);
the end-to-end test harness is [Swarm E2E testing](e2e-swarm.md).

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
aq pool set-lifecycle --project-id agent-queue \
    --profile-id worker-standard-high --lifecycle pool

aq pool scale --project-id agent-queue \
    --profile-id worker-standard-high --min 0 --max 3
```

Both write the **project-scoped** override, never the system profile:

```
~/.agent-queue/vault/projects/<project>/agent-types/<profile>/profile.md
```

If no override exists yet it is seeded from the system profile at
`vault/agent-types/<profile>/profile.md`, with the frontmatter `id` rewritten
to `project:<project>:<profile>` so the copy cannot upsert the system row.
That scoping is why the same profile can be a pool in one project and stay on
push in another.

The write order inside the command is deliberate: the `agent_profiles` row is
updated **first** so the very next tick honours the new value, then the vault
markdown is written and re-synced. The vault is the source of truth
(swarm-work-model §14), so a DB-only write would be reverted by the next vault
sync. A read-only vault degrades the command to the non-durable DB-only
behaviour with a logged warning rather than failing outright — worth knowing,
because the change will then silently disappear on the next sync.

Both commands are admin surfaces. A worker session's task-scoped token gets
`out of scope: pool_scale`.

### The pool-only config keys

These live in the profile's own `## Config` block, not in `config.yaml`, and
the parser rejects them on a `task` or `named` profile:

| Key | Meaning |
|---|---|
| `min_active` | Floor. `0` means "no idle workers when the queue is empty". |
| `max_active` | Ceiling for this profile. `null` = no profile limit (the project cap still applies). |
| `max_claims_per_session` | How many tasks one session may claim before it retires. Ignored while `swarm.fresh_context_per_task` is true, which pins the effective cap at 1. |

---

## 3. How the size is chosen

`src/scheduler.py:size_pools` is pure — no I/O, no clock — and runs once per
cascade tick against a single measurement pass:

```
want    = running_busy + ready
desired = clamp(want, min_active, max_active)          # max_active None = unbounded
desired = max(desired, running_busy + starting)        # never undercut work in flight
```

`ready` is READY tasks routed to that profile, plus the project's *unrouted*
READY tasks if this profile is the project default.

Then two caps bind, in order:

1. **The project cap** — `projects.max_concurrent_agents`, summed across *all*
   of that project's pools. This is why per-profile maxima are allowed to
   over-subscribe: six profiles capped at 3+2+2+2+1+1 = 11 under a project cap
   of 8 does not mean 11 workers, it means whichever pools want capacity
   fair-share the 8.
2. **`swarm.max_starts_per_tick`** (default 2) — starts are handed out one at
   a time, round-robin across every pool that still wants more, so a saturated
   cap shares the remaining headroom instead of starving whichever pool sorts
   last.

There is no global (all-projects) pool cap; `global_cap` is passed as `None`.

**Scale-down** is deliberately reluctant. It only ever drains *idle* sessions,
oldest first, only after the pool has been continuously in surplus for
`swarm.scale_down_grace` seconds (default 120), and at most
`swarm.max_drains_per_tick` (default 5) per tick. A drain sets
`desired_state='stopped'`; the session finishes what it is doing and its next
claim attempt returns `drain_requested`. A busy worker is never drained
mid-task.

`aq pool scale --now` is the exception: it terminates idle sessions above the
new effective max immediately, oldest first, skipping the grace window.

### Reading the shape

```bash
aq pool status                      # every project
aq pool status --project-id agent-queue
aq session list --lifecycle pool --live
```

`pool status` gives one row per `(project, profile)` with `min_active`,
`max_active`, the computed `desired`, the supply split
(`running_idle` / `running_busy` / `starting` / `draining`), the `ready`
demand, and one entry per live instance with its task and idle time.

### Reading the history

`pool status` shows the current shape, never the fact that it changed. Only
`pool.scaled` is written to the event table, and it is the answer to "why did
a worker appear at 03:14?":

```bash
aq system get-recent-events --event-type 'pool.*' --since 1h
aq system get-recent-events --event-type pool.scaled --project-id agent-queue
```

| Event | Emitted when | Persisted |
|---|---|---|
| `pool.scaled` | the sizer started or drained sessions | yes |
| `pool.session_started` | a launch succeeded | bus + WebSocket only |
| `pool.session_claimed` | a session took a task | bus + WebSocket only |
| `pool.session_drained` | a session was torn down (carries the reason) | bus + WebSocket only |
| `pool.session_quarantined` | a session went terminal-bad | bus + WebSocket only |
| `pool.bounds_changed` | `pool scale` | bus + WebSocket only |
| `pool.lifecycle_changed` | `pool set-lifecycle` | bus + WebSocket only |
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
`quarantined_reason` on the row — a bare deadline left operators staring at a
stalled pool with nothing to act on.

Quarantining failures:

- the configured session provider cannot be constructed;
- the profile's `harness` is unknown to the registry;
- the workspace resolved to the **base checkout** (a pool session may not run
  there — see `src.orchestrator.base_workspace`);
- the harness process died during startup — the reason carries the last ~400
  characters of the captured startup output, read once and then reused, so one
  dead harness does not become a wall of identical stack traces;
- the session started but its `sessions` row could not be written;
- any other exception out of acquisition, token mint, spec build or launch.

**Starvation is not quarantine.** "no `project-repo` workspace kind" and "no
free workspace" return without setting a backoff, because the next tick may
genuinely find a workspace freed. A pool that is flat at zero with no
`quarantined_reason` is starved, not broken — look at workspaces, not at the
harness:

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
| `pools.preparing_stuck` | ERROR | yes | A session sat in `claim_phase` `claiming`/`preparing` past `2 × prepare_timeout` (the git-reset window). The fix releases the claim as `prepare_failed`. |
| `pools.disabled` | WARN | no | Pool profiles exist but `swarm.enabled` is false — see §1. Report-only on purpose. |
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

> **Do not `aq agent delete` worker rows to "clean up" after a cutover.**
> `create_automatic_agent` refuses to insert *any* new automatic worker while
> a single soft-deleted `role='worker'` row exists — the tombstone is how the
> roster records "the operator sizes this by hand now"
> (`src/database/queries/agent_queries.py:49`). One deletion therefore caps the
> fleet permanently at whatever `IDLE` definitions already exist:
> `_launch_pool_session` can still *reuse* them, but it can never create
> another one, and a pool with nothing left to reuse silently stops growing.
> Retiring a row (`state=RETIRED`) or disabling it (`aq agent edit --no-enabled`)
> has no such effect. Prefer leaving redundant fixed rows `IDLE` — pools reuse
> compatible idle definitions regardless of which profile originally created
> them, so yesterday's push agents become today's pool capacity for free.

---

## 6. Rolling back

The rollback is one command per profile and needs no restart:

```bash
aq pool set-lifecycle --project-id agent-queue \
    --profile-id worker-standard-high --lifecycle task
```

What it does, in order:

1. Writes `lifecycle: task` into the project override *and* clears
   `min_active`, `max_active` and `max_claims_per_session` — the parser rejects
   those keys on a task profile, so leaving them behind would make the override
   fail its next vault sync.
2. Marks every live pool session for that profile `desired_state='stopped'`.
   They are **not** killed: a session holding a task keeps it and releases it
   through the normal close path; the next claim attempt returns
   `drain_requested`.
3. Emits `pool.lifecycle_changed` and one `pool.session_drained` per session.

From the next tick the push scheduler assigns that profile's tasks again.

Note the asymmetry, because it is the mechanism that makes rollback per-project:
a project-scoped row with a lifecycle *other than* `pool` does not merely fail
to add a pool — it **removes** the system profile's pool for that project. The
override is an explicit opt-out, not just an absence.

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

For a project moving several worker profiles at once. Every step is an admin
surface; a task-scoped worker token cannot run any of them.

**1. Check the preconditions.**

```bash
aq doctor --check pools.disabled --check pools.orphan_agents
aq system get-config --section swarm
aq project get --project-id agent-queue        # note max_concurrent_agents
```

**2. Flip lifecycle, then bounds, one profile at a time.** Do the smallest,
least-loaded profile first and watch a full tick before continuing.

```bash
P=agent-queue
aq pool set-lifecycle --project-id $P --profile-id worker-standard-low --lifecycle pool
aq pool scale         --project-id $P --profile-id worker-standard-low --min 0 --max 1
aq pool status --project-id $P
```

Keep `min 0` for every pool unless you are deliberately paying for a warm
worker: a floor above zero holds sessions open against an empty queue.

Sum the maxima against the project cap knowingly. Over-subscription is a
fair-share arrangement, not a promise — with a cap of 8 and maxima summing to
11, three profiles' worth of demand is what gets squeezed, and which three
depends on arrival order.

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
cutover is the vault override files and whatever you write down:

```bash
git -C ~/.agent-queue/vault status --short   # if the vault is version-controlled
```

### Worked example — this repo's own cutover

The bounds agreed for the `agent-queue` project, under a project cap of 8.
`triage`, `supervisor` and `worker-deep` stay on `lifecycle: task`.

| Profile | Harness | `min` | `max` |
|---|---|---|---|
| `worker-standard-high` | claude | 0 | 3 |
| `worker-standard` | claude | 0 | 2 |
| `worker-standard-low` | claude | 0 | 1 |
| `worker-standard-high-codex` | codex | 0 | 2 |
| `worker-deep-codex` | codex | 0 | 2 |
| `worker-standard-low-codex` | codex | 0 | 1 |

The maxima sum to 11 against a cap of 8 — deliberate over-subscription, so a
quiet profile's headroom is usable by a busy one (§3).

```bash
P=agent-queue
for spec in worker-standard-low:1 worker-standard-low-codex:1             worker-standard:2 worker-deep-codex:2             worker-standard-high-codex:2 worker-standard-high:3; do
  profile=${spec%:*}; max=${spec#*:}
  aq pool set-lifecycle --project-id $P --profile-id "$profile" --lifecycle pool
  aq pool scale         --project-id $P --profile-id "$profile" --min 0 --max "$max"
done
aq pool status --project-id $P
```

Ordered smallest-first on purpose: the first two lines are the cheap probe
that the vault override, the harness and the claim loop all work, before the
profiles that carry the real load are moved.

---

## 8. Symptom → cause

| Symptom | Look at |
|---|---|
| Tasks sit in READY, nothing launches | `aq doctor --check pools.disabled`; `swarm.enabled` and `sessions.enabled` |
| `pool status` shows a pool flat at 0, with `quarantined_reason` | §4a — harness, provider, base checkout, or a dead startup |
| `pool status` shows a pool flat at 0, no quarantine reason | starved: no `project-repo` kind, or no free workspace |
| `desired` is below `ready` | the project cap, or `max_active`, is binding — not a bug |
| Workers appear and vanish every minute | a quarantining launch failure; read `quarantined_reason` (§4a) |
| A session holds a task that is already closed | `aq doctor --check pools.stuck --fix` |
| A claim never completes | `aq doctor --check pools.preparing_stuck --fix` (releases as `prepare_failed`) |
| Pools stopped growing and nothing is quarantined | a soft-deleted worker row is fencing `create_automatic_agent` — §5 |
| A task is BLOCKED with `needs_attention: session_stall` | the session was quarantined (§4b); clear the task by hand |
| The pool never scales down | `scale_down_grace` has not elapsed, or every session is busy |

---

## See also

- [`docs/specs/config.md` §4.11](../specs/config.md) — every `swarm:` key,
  its default and its validation.
- [Swarm E2E testing](e2e-swarm.md) — a real daemon on real PostgreSQL through
  the whole claim protocol in ~2½ minutes, no LLM.
- [Resource gating](resource-gating.md) — the box-wide caps that decide how
  much CPU the pool's sessions may actually use.
- [swarm-work-model design](../superpowers/specs/2026-08-28-swarm-work-model-design.md)
  §10–§12, §17.
