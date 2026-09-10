# Global worker pools

<!-- aq:historical -->
> **Historical design record.** This spec describes one feature as it was
> designed, not as the code stands today. Start at [the documentation
> home](../../README.md) for current behaviour; see [historical
> material](../../history/README.md).

**Date:** 2026-09-08
**Status:** implemented (2026-09-08) — sizing, placement, bounds, doctor, CLI and
API surfaces have shipped; the dashboard re-keying in §6.4 has not
**Supersedes:** the per-project runtime keying in
`docs/superpowers/specs/2026-08-28-swarm-work-model-design.md` §11
**Primary code:** `src/scheduler.py` (`size_pools`), `src/orchestrator/pools.py`,
`src/commands/ops_commands.py`, `src/doctor/pool_checks.py`,
`dashboard/src/pages/agents/`

## Problem

A pool is meant to be a fleet of durable workers shared across every project.
The *configuration* already says so — profiles are global
(`_pool_profiles` filters `":" not in p.id`, `pools.py:100`), and `pool_scale`
explicitly deprecates its `project_id` argument
(`ops_commands.py:334`: *"project_id is deprecated and ignored"*). But the
*runtime* does not: `_measure_pools` iterates every ACTIVE project and mints a
`PoolKey(project.id, profile_id)` per project (`pools.py:171`), then copies the
profile's bounds into each one:

```python
bounds[key] = (profile.min_active or 0, profile.max_active)   # pools.py:174
```

So one profile with N active projects is N independent pools with N independent
floors and ceilings:

| Setting | Intended | Actual with 5 active projects |
|---|---|---|
| `min_active: 2` | 2 resident workers | **10** resident workers |
| `max_active: 4` | at most 4 workers | at most **20** workers |
| demand | fleet-wide ready count | `count_ready_by_profile(project.id)`, per project |
| ceiling | a box-wide bound | `project.max_concurrent_agents` (default **2**, `models.py:382`) |

And there is no box-wide bound at all: `global_cap=None` is hardcoded at the
one call site (`pools.py:229`) even though `size_pools` takes the parameter.

`_pool_profiles`' own docstring states the per-project fan-out as deliberate
(*"Sizing stays per project at runtime … only the configuration is global"*).
This spec reverses that decision.

## What binds a worker to a project today

Only three things, and the change below touches none of the first two:

1. **The workspace.** `_launch_pool_session` acquires a `project-repo` worktree
   slot for one project *at launch*, and the session's `work_dir` never
   changes. A claim only re-points the branch inside that same slot
   (`reset_slot_for_task` in `_prepare_and_activate_locked`).
2. **The scope fence.** The session token is minted with `project_id`;
   `_assert_task_in_scope` (`claim_commands.py:88`) refuses any task outside
   it, `select_ready_for_profile` filters `_frontier_where(project_id)`, and
   the long-poll waiter filters on `project_id`.
3. **Cosmetics** — `pool_session_name(profile, project, nonce)`, `pool.scaled`
   payloads, `aq pool status` rows, dashboard `poolAddress(projectId, profileId)`.

A worker therefore lives and dies inside one project. That stays true here.
Making workers genuinely project-mobile — `work_dir` mutable, workspace
acquired at claim time, the fence replaced by a per-claim fence, the harness
re-rooted between claims — is a separate, much larger change and an explicit
non-goal (see §9).

It is affordable to leave alone because `swarm.fresh_context_per_task` defaults
to **true** (`config.py:1956`), pinning `cap=1` in `_pool_context_claim_cap`: a
worker takes one task, returns `SESSION_EXHAUSTED`, and drains. Under the
default configuration a worker's project binding only has to last one task.

## 1. Design

Two changes, both in the sizing half of the system:

- **Sizing becomes global.** `PoolKey` loses `project_id`. Supply, demand and
  bounds are aggregated per profile across every eligible project, so
  `min_active` / `max_active` finally mean what an operator reads them to mean.
- **Placement becomes explicit.** The sizer no longer knows about projects, so
  a new pure step decides *which* project each authorised start goes to, and
  *which* project each drain comes from. Per-project caps, workspace
  availability, quarantine and the new warm floor are all placement inputs.

```
_measure_pools ──▶ size_pools ──────▶ [start × n, drain × m]   (per profile)
   (per project        (global,              │
    observation)        pure)                ▼
                                    place_pool_actions            (pure)
                                    ├─ eligibility: active, has profile,
                                    │  not quarantined, workspace capacity,
                                    │  under project cap
                                    ├─ order: warm floor deficit, then
                                    │  unserved deficit, then fewest live
                                    └─ drains: never below a warm floor,
                                       largest idle surplus first
                                             │
                                             ▼
                                    _launch_pool_session(project, profile)
                                    db.update_session(desired_state="stopped")
```

### 1.1 `size_pools` — global keying

`PoolKey` becomes a single field:

```python
@dataclass(frozen=True)
class PoolKey:
    profile_id: str
```

`size_pools` drops the `project_caps` parameter entirely — caps are a placement
concern now — and keeps everything else. `PoolSupply` gains a per-project
breakdown so the placement step can act on the same observation without a
second measurement pass:

```python
@dataclass
class PoolSupply:
    running_idle: int = 0
    running_busy: int = 0
    starting: int = 0
    draining: int = 0
    idle_session_ids: list[str] = field(default_factory=list)   # oldest first
    by_project: dict[str, "PoolProjectSupply"] = field(default_factory=dict)
```

`PoolProjectSupply` carries the same five counters plus that project's
`idle_session_ids`. The aggregate fields stay authoritative for sizing so the
existing `desired = clamp(busy + ready, min, max)` then
`max(desired, busy + starting)` logic is unchanged — only the domain it runs
over shrinks from N×M keys to M keys.

The `busy + starting` floor keeps its meaning fleet-wide: a launch in flight in
project A and a task held in project B both protect the fleet from being
undercut mid-task.

### 1.2 `place_pool_actions` — new pure function

New in `src/scheduler.py`, next to `size_pools`, with no I/O and no clock:

```python
@dataclass(frozen=True)
class PlacementCandidate:
    project_id: str
    ready: int                  # ready tasks for this profile in this project
    live: int                   # idle + busy + starting pool sessions, this profile
    project_live_total: int     # all pool sessions in this project, any profile
    project_cap: int | None     # project.max_concurrent_agents
    workspace_capacity: int     # count_available_workspaces
    quarantined: bool           # (project, profile) inside its backoff window
    warm_floor: int             # profile.min_per_project
    idle_session_ids: tuple[str, ...]   # oldest first

def place_pool_actions(
    *,
    actions: list[PoolAction],
    candidates: dict[PoolKey, list[PlacementCandidate]],
) -> tuple[list[PlacedStart], list[PlacedDrain], list[PlacementStarvation]]:
```

**Start eligibility.** A candidate may receive a start only if all hold:

- `not quarantined` — checked *before* the budget is spent, not after (§1.4);
- `workspace_capacity > 0`;
- `project_cap is None or project_live_total < project_cap`.

**Start ordering.** Starts are handed out one at a time, re-sorting after each
so the budget spreads rather than piling onto the first project:

1. candidates below their `warm_floor` (`live < warm_floor`), most short first;
2. then by unserved deficit `ready - (idle + starting)`, largest first;
3. then by fewest `live` (spread over concentrate);
4. then `project_id` ascending, so the function is deterministic and testable.

A candidate with an idle worker already sitting in it and no unserved deficit
is skipped: that idle worker will claim the next ready task itself, and
launching beside it just creates a drain candidate two minutes later.

**Drain selection** replaces today's globally-oldest-first. For each drain
action, in order:

1. exclude any candidate at or below its `warm_floor`;
2. pick the candidate with the largest idle surplus relative to its own
   demand — `idle - max(0, ready - starting)` — largest first, `project_id` as
   tie-break;
3. within that candidate, oldest idle session first.

This is what stops a quiet project's warm worker from being the first thing
reaped simply because it is idle and old (§7.2).

**Starvation reporting.** When `size_pools` authorises a start and no candidate
is eligible, the function returns a `PlacementStarvation(key, reason)` rather
than silently dropping it. Today that condition is invisible: the start is
attempted, `_launch_pool_session` returns `None`, and the only trace is a
`logger.warning` about a starved pool. §6.3 turns these into an event.

### 1.3 `_measure_pools` and `_reconcile_pools`

`_measure_pools` keeps its per-project loop — it is the observation, and one
`count_ready_by_profile` + one `list_sessions` per project is already the
cheapest shape — but folds each project's numbers into the aggregate key *and*
records the per-project breakdown and the placement inputs. Its return becomes
a single dataclass rather than a six-tuple, because the tuple is already at the
limit of what a caller can destructure correctly and this adds to it:

```python
@dataclass
class PoolMeasurement:
    supply: dict[PoolKey, PoolSupply]
    demand: dict[PoolKey, int]
    bounds: dict[PoolKey, tuple[int, int | None]]
    profiles: dict[PoolKey, AgentProfile]
    candidates: dict[PoolKey, list[PlacementCandidate]]
    projects: dict[str, Project]
```

`_reconcile_pools` becomes: measure → `size_pools` → `place_pool_actions` →
execute. `workspace_capacity` needs one `count_available_workspaces` per
project per tick, which the launch path pays anyway; it moves earlier so an
ineligible project never consumes start budget.

Note that `project_live_total` counts **pool** sessions only, matching today's
`used_project` accounting in `size_pools`. Task-lifecycle sessions also consume
`max_concurrent_agents` in the push scheduler, so a project running both push
and pull work can exceed its nominal cap. That is pre-existing, it is not made
worse here, and fixing it is out of scope — recorded in §9.

### 1.4 Quarantine

`_pool_quarantine` stays keyed `(project_id, profile_id)`. A quarantine is
always project-specific — an unknown harness for that project, a startup death
in that checkout, a base-checkout refusal against that repo — so collapsing it
to the profile would let one broken project disable a healthy fleet.

The ordering changes: today `_reconcile_pools` picks a key and *then* checks
the quarantine window (`pools.py:~235`), so a quarantined key silently wastes
its share of `max_starts_per_tick`. Under a global key that would be much
worse — one broken project could burn the entire fleet's start budget every
tick. Quarantine therefore becomes an eligibility predicate consumed by
`place_pool_actions`, and the post-hoc check goes away.

## 2. New configuration

### 2.1 `min_per_project` (pool profile key)

A per-project warm floor, defaulting to `0`. Parsed alongside `min_active` /
`max_active` in `src/profiles/parser.py` (the `_POOL_KEYS` tuple, the two
positive-integer validation loops at `parser.py:684` and `:1113`), stored on
`agent_profiles`, and carried through `profile_queries.py` and
`profiles/sync.py` like its siblings.

```markdown
## Config
lifecycle: pool
min_active: 4
max_active: 12
min_per_project: 1
```

Interactions:

- The effective global floor is `max(min_active, Σ min_per_project over
  eligible projects)`. A `min_per_project` that the global `min_active` cannot
  fund raises the floor rather than being silently ignored.
- If that effective floor exceeds `max_active`, the configuration is
  contradictory. Sizing clamps to `max_active` (existing behaviour, no new
  failure mode) and a new doctor check names it (§5).
- `min_per_project` applies only to *eligible* projects. A project with no
  workspace capacity or a live quarantine does not hold a reservation open
  against the rest of the fleet.

This is the knob that buys back what §7.1 costs: a project that genuinely needs
a resident worker gets one, at a cost the operator states explicitly, instead
of every project getting one by accident.

### 2.2 `swarm.global_max_active`

```python
@dataclass
class SwarmConfig:
    ...
    global_max_active: int | None = None   # None → resources.max_concurrent_agents
```

Fed to `size_pools`' `global_cap`, which has been a dead parameter since it was
written. `None` resolves to `resources.max_concurrent_agents` (default 8) so
the box-wide bound exists by default; an explicit integer overrides it and
`0` is rejected by `SwarmConfig.validate` along with the other non-negative
checks.

An explicit knob rather than reading `resources.max_concurrent_agents`
directly, because that value also drives the per-session xdist worker share and
the test-slot semaphore (`config.py:1911`); an operator tuning pool size should
not silently re-tune test parallelism.

### 2.3 `swarm.max_starts_per_tick`

Unchanged at 2, but its meaning narrows: today it is 2 starts per tick spread
round-robin across N project keys, so a 5-project fleet effectively ramps at up
to 2 per tick *per profile* with N keys competing. Globally it is 2 per profile,
period — a 0→8 ramp takes 4 ticks (~20 s) instead of being parallelised.
Release notes should point at this; no default change is proposed, since 20 s to
full fleet is well inside the noise of a harness boot.

## 3. What this does and does not do to context

Worth stating plainly, because it is the most likely misreading of the change.

**Unchanged.** `max_claims_per_session`, `_pool_context_claim_cap`
(`claim_commands.py:337`) and the `SESSION_EXHAUSTED` path are untouched. With
`swarm.fresh_context_per_task: false` and a profile `max_claims_per_session: N`,
a worker still chains N tasks on one warm harness conversation, in one project,
exactly as today. And with the shipped default (`true`) there is no cross-task
context to lose in the first place.

**Changed.** `min_active` stops being a per-project warm floor. Today
`min_active: 2` parks two resident workers in *every* project; globally it parks
two *somewhere*. A task arriving in a project with no resident worker pays a
cold launch — workspace acquisition, harness boot, prime — instead of landing
on a warm one. `min_per_project` (§2.1) is the explicit way to buy that back
where it matters.

**The durable answer to cross-task continuity remains the memory tiers**, not
session warmth. L1 facts and L2 topic memory survive worker death, placement
and daemon restarts; a warm harness conversation survives none of them. Pool
warmth is a latency optimisation, and this spec treats it as one.

**Not context, easily mistaken for it.** `_launch_pool_session` already prefers
an idle `agents` row with a matching profile (`pools.py:~333`), and
`select_ready_for_profile` orders by `affinity_agent_id` first. That gives
*identity* continuity — same agent name, same affinity routing — with a brand
new harness process. Both survive this change untouched.

## 4. Upgrade behaviour

The bounds re-scoping is a real, intended reduction in fleet size, and on a
multi-project install it is large: `max_active: 4` across 5 projects goes from
an effective 20 to 4. Left unannounced it reads as a throughput regression.

The bounds are **not** auto-multiplied on upgrade — that would preserve the
very bug this removes. Instead:

- **One-shot doctor check `pools.global_bounds_migration`** (info severity):
  for each pool profile, report the pre-upgrade effective ceiling
  (`max_active × eligible projects`) against the new one, and suggest the
  `max_active` that preserves current capacity. It self-resolves once an
  operator has run `aq pool scale` or acknowledged it, so it is not permanent
  noise. No `--fix`: choosing a fleet size is an operator decision.
- **One `pool.bounds_rescoped` event** per profile on the first reconcile tick
  after upgrade, so `aq system get-recent-events` answers "why did my fleet
  shrink at 03:14?" — the same reasoning that put `pool.scaled` on the bus.
- **Release note** carrying the arithmetic and the `min_per_project` knob.

## 5. Doctor

| Check | Severity | Detail |
|---|---|---|
| `pools.global_bounds_migration` | info | pre/post effective ceiling per profile; suggested `max_active` (§4) |
| `pools.floor_exceeds_max` | warn | `max(min_active, Σ min_per_project) > max_active` — the floor cannot be funded |
| `pools.placement_starved` | warn | a profile with unserved demand and no eligible project for ≥ 5 min; names the blocking reason per project (quarantined / no workspace / at cap) |

The existing `pools.stuck`, `pools.orphan_agents`, `pools.preparing_stuck` and
`pools.stale_worktree_checkouts` are session- and agent-scoped, not pool-key
scoped, and need no change.

## 6. Surfaces

### 6.1 `pool_status` / `PoolStatusRow`

One row per profile, with the per-project detail nested rather than dropped —
"where are my workers actually running" must stay answerable, and it is the
first thing an operator asks after this change.

```python
class PoolProjectStatus(BaseModel):
    project_id: str
    ready: int
    running_idle: int
    running_busy: int
    starting: int
    draining: int
    max_concurrent_agents: int | None = None
    workspace_capacity: int
    quarantined_until: float | None = None
    quarantined_reason: str | None = None

class PoolStatusRow(BaseModel):
    profile_id: str
    enabled: bool = True
    min_active: int
    max_active: int | None = None
    min_per_project: int = 0
    desired: int
    running_idle: int
    running_busy: int
    starting: int
    draining: int
    ready: int
    projects: list[PoolProjectStatus] = []
    instances: list[PoolInstanceStatus] = []
```

`project_id`, `quarantined_until` and `quarantined_reason` leave the top level
and move into `projects[]`, where they always belonged — a quarantine was never
a property of a global pool. `PoolInstanceStatus` gains `project_id`.

`pool_status --project-id` is retained as a **view filter** on `projects[]` and
`instances[]`, never as pool identity.

`PoolProjectCap` on `PoolScaleResponse` (`api/models/task.py:702`) already
models exactly this shape for `pool_scale` and needs only
`effective_max_active` recomputed against the global ceiling.

These are breaking model changes: regenerate with
`./scripts/regenerate-api-client.sh --offline` then
`./scripts/regenerate-ts-client.sh --from-file`, per CLAUDE.md. Never hand-edit
`packages/aq-client/`.

### 6.2 CLI

`format_pool_table` (`src/cli/formatters.py:1173`) drops its leading **Project**
column and gains a **Projects** column summarising placement compactly
(`web:2i/1b  api:1i`), keeping the one-line-per-pool shape. `aq pool status
--project-id X` renders the filtered breakdown. `aq pool scale` is unchanged —
it was already global, and `_deprecated_project_id` can finally be deleted in
the release that ships this.

### 6.3 Events

- `pool.scaled` keeps `project_id` (placement chose one) and gains
  `placement_reason` — `warm_floor` / `deficit` / `spread`.
- `pool.placement_starved` (new): `profile_id`, `wanted`, and per-project
  blocking reasons. This is the condition that is invisible today.

### 6.4 Dashboard

- `poolAddress(projectId, profileId)` → `poolAddress(profileId)`; `POOL_PREFIX`
  keys shorten. `poolEntries` joins sessions to profiles rather than to
  (project, profile) pairs, and each entry carries the project breakdown.
- `AddPool.tsx` loses its project `<select>` and its
  `pools.find(p => p.project_id === projectId && ...)` duplicate check — a pool
  is now identified by profile alone.
- The agents page's pool detail gains a per-project table fed by `projects[]`.
- `splitBusyPoolEntries` is unaffected (`running_busy` stays an aggregate).

## 7. Constraints this accepts

Recorded so they are not rediscovered as bugs.

1. **No per-project warm floor by default.** §3. Mitigated by
   `min_per_project`, which costs fleet slots when used.
2. **Warmth follows demand.** Deficit-ordered placement concentrates workers in
   the busiest project. The drain rule in §1.2 stops a quiet project's worker
   from being reaped merely for being idle and old, but nothing keeps a worker
   warm in a project with no work — nor should it, above `min_per_project`.
3. **Cross-project isolation weakens.** Today a 500-task backlog in project A
   cannot touch project B's pool, because B has its own. Under one fleet, A
   absorbs capacity up to `max_active`. `project.max_concurrent_agents` remains
   a ceiling on A's share and `min_per_project` a floor under B's, but there is
   no proportional fair-share — see §9.
4. **Slower cold ramp under burst.** §2.3.
5. **Workers remain project-bound for their lifetime.** §9.

## 8. Testing

Pure functions first — `size_pools` and `place_pool_actions` are where the
behaviour lives and neither needs a database.

- `tests/test_pool_sizing.py` — rewrite for single-field `PoolKey`; new cases:
  aggregation across projects, `Σ min_per_project` raising the effective floor,
  the floor clamped by `max_active`, `global_max_active` binding.
- `tests/test_pool_placement.py` (new) — start ordering (warm floor before
  deficit before spread), every eligibility predicate in isolation, the
  idle-worker skip, drain selection never crossing a warm floor, determinism on
  ties, and `PlacementStarvation` when nothing is eligible.
- `tests/test_pool_reconciler.py` — `PoolMeasurement`, quarantine consulted
  before the budget is spent, `pool.scaled` payload, `pool.placement_starved`.
- `tests/test_pool_doctor.py` — the three new checks.
- `tests/test_swarm_surface.py`, `tests/test_cli_formatters.py`,
  `tests/test_session_commands.py` — response and table shapes.
- `tests/test_agent_profiles.py` — `min_per_project` parse, validation, sync.
- `tests/test_pool_agent_routing.py`, `tests/test_pool_lifecycle_integration.py`,
  `tests/test_task_session_pool_integration.py`,
  `tests/perf/test_claim_statements.py` — `PoolKey` construction sites.
- Migration test for the `agent_profiles.min_per_project` column.
- End to end: `scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh` — mandatory
  after any change to claims or pools.

Focused runs while iterating, one area run at the end:

```bash
aq test tests/test_pool_sizing.py tests/test_pool_placement.py
aq test tests/test_pool*.py tests/test_swarm_surface.py
```

## 9. Non-goals

- **Project-mobile workers** (the "Option B" shape): a worker outliving its
  project and switching repos between claims. Requires the claim query to scan
  across projects, workspace acquisition to move from launch to claim time,
  mutable session `work_dir`, the token's `project_id` fence replaced by a
  per-claim fence, and the harness re-rooted or restarted on a switch. With
  `fresh_context_per_task: true` as the default it buys close to nothing over
  this design.
- **Proportional fair-share across projects.** Placement here is
  floor-then-deficit. Weighting by project priority or token budget is a
  natural follow-up once the global key exists, and is deliberately not in the
  first cut.
- **Reservations beyond a floor.** `min_per_project` reserves warm capacity,
  not a share of burst capacity.
- **Unifying push and pull accounting against `max_concurrent_agents`.** §1.3.

## 10. Phasing

Each phase is independently shippable and leaves the tree green.

1. **Sizing and placement.** `PoolKey` re-keying, `PoolSupply.by_project`,
   `place_pool_actions`, `PoolMeasurement`, quarantine as an eligibility
   predicate. `pool_status` keeps its current wire shape by projecting the
   breakdown back into per-project rows, so no surface moves yet. This alone
   fixes the reported problem.
2. **Bounds.** `swarm.global_max_active` wired to the dead `global_cap`;
   `min_per_project` through parser → table → migration → queries → sync →
   placement; the three doctor checks; `pool.bounds_rescoped`.
3. **Surfaces.** `PoolStatusRow` reshaping, openapi + both client
   regenerations, CLI table, dashboard keying and the new per-project table,
   `pool.placement_starved`, and deletion of `_deprecated_project_id`.
