# Session desired state — design

*2026-08-27. Recommendation #2 from
`docs/analysis/2026-08-26-session-runtime-vs-gascity.md`.*

## Problem

`sessions.state` is a **runtime projection** — what the reconciler last
observed. There is nowhere to record what the daemon *wants*. With one
column serving both roles, "this session is sleeping" and "this session
should be sleeping" are the same value, so a wake branch in `_step_named`
would fight the idle-drain branch on every 5 s tick.

That is the actual reason `_step_named` is drain-only, and its docstring
says so:

> v1 scope: drain idle sessions to `sleeping`. Starting and recycling named
> sessions needs the message routing that [[design/supervisor-agent]] owns,
> so this deliberately converges in one direction only rather than
> half-implementing wake semantics.

The routing half is real, but it is not the blocker it was: `SessionLens`
already owns a working named-session cold start. The blocker is
**representational**. Gas City's `lifecycle_projection.go` splits BaseState
/ DesiredState / RuntimeProjection; we have collapsed all three into one
`state` column.

Turning `sessions.enabled` on before fixing this means named sessions that
go to sleep and never wake.

## Non-goals

Profile-declared session pools, phantom row creation (starting a session
with no row), and recycle semantics. All become writable once intent is
representable; all need the supervisor-agent routing story settled first.

## Approaches considered

1. **`desired_state` column** — one Text column beside `state`. Explicit,
   greppable, survives restart. **Chosen.**
2. **Derive intent from existing columns** (`sleep_reason` non-NULL ⇒ meant
   to sleep). No migration, but it overloads a forensics field and cannot
   express "wake this" at all.
3. **Separate `session_intents` table** — Gas City's full split. The right
   shape once intent grows a spec, a schedule, and a replica count. Today
   intent is one enum, so it is a join for nothing. This is where approach
   1 grows to if it needs to.

## Design

### Schema

```
sessions.desired_state  Text NOT NULL server_default 'running'
```

Values: `running | sleeping | stopped`. Mirrored onto `SessionRecord`,
`_row_to_session`, `create_session`, and a `desired_state=` filter on
`list_sessions`.

The migration **backfills**, and this is load-bearing: a bare default of
`running` would mark every already-dead row in the table as wanted, and the
first tick after enabling the runtime would try to resurrect the lot.

| observed `state`          | backfilled `desired_state` |
|---------------------------|----------------------------|
| `stopped`, `quarantined`  | `stopped`                  |
| `sleeping`                | `sleeping`                 |
| everything else           | `running`                  |

### Who writes intent

Intent is written by whoever *forms* it. The reconciler observes; it only
writes intent when it is itself the one deciding (idle drain, terminal
verdict).

| Event | `desired_state` |
|---|---|
| lens cold-start, task-session create | `running` |
| `_step_named` idle drain | `sleeping` |
| drain-ack, quarantine, orphan kill, task close | `stopped` |
| `aq session kill` | `stopped` |
| `aq session sleep` | `sleeping` |
| `aq session wake` | `running` |

The idle-drain row is what stops the flap: a drained session stops being
wanted at the same moment it stops running, so the up-branch does not
immediately undo the down-branch. **Wake is an explicit act** — the lens on
an inbound message, or an operator — never an inference.

### Convergence

`_step_named` gains a second branch. Rows with `lifecycle='named'`,
`desired_state='running'`, and a non-live `state` are started.

The reconciler does **not** grow its own cold start. It calls an injected
starter satisfying a narrow protocol:

```python
class NamedSessionStarter(Protocol):
    async def ensure_started(
        self, *, kind: str, target_id: str, project_id: str | None
    ) -> bool: ...
```

`SessionLens` already satisfies it. The lens owns API-token minting, the
global-supervisor special cases, the `projects` FK stub, and work_dir
resolution — roughly 150 lines that must not be forked. The protocol keeps
the reconciler off the lens's concrete type.

The starter is **optional**. `None` means the up-branch no-ops with a debug
log, so every existing test that constructs a reconciler without one keeps
its current behavior.

Retries reuse the stall ladder's existing budget: `bump_session_restarts`,
`sessions.max_restarts`, `sessions.restart_backoff_seconds`. A named
session that cannot start quarantines at the cap instead of retrying every
5 s forever.

The runtime name → messaging address mapping (`n-supervisor--<pid>` →
`supervisor-<pid>`) is the inverse of the lens's existing
`_resolve_runtime_session_name`; the reconciler asks the lens rather than
re-deriving it.

### Error handling

- Starter raises → logged, restart counter bumped, next tick retries after
  backoff. Never destructive.
- Starter returns `False` (profile missing, harness unregistered) → same
  path. A permanently misconfigured supervisor reaches quarantine and stops
  costing a start attempt per tick.
- `PartialListError` deferral applies unchanged: an incomplete enumeration
  defers the whole prefix, up-branch included. Unknown is not dead, and it
  is not alive either.

### Testing

Reconciler unit tests against the fake provider:

- idle drain writes **both** `state='sleeping'` and `desired_state='sleeping'`
- `sleeping` + `desired='running'` calls the starter
- `sleeping` + `desired='sleeping'` does not
- a failing starter bumps `restarts` and quarantines at `max_restarts`
- terminal verdicts leave `desired_state='stopped'`, so nothing resurrects
- no starter injected → up-branch is a no-op

Plus a migration test asserting the backfill mapping, and command tests for
`session sleep` / `session wake` / `session kill`.
