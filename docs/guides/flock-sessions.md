# Every agent session belongs to the flock

Every AQ agent process must have a committed `sessions` registration before
its provider spawns it. The flock lists executions independently of durable
worker definitions: a project supervisor needs no worker capacity row to be
visible, and two executions linked to one worker remain two rows.

## Launch contract

`src/sessions/launch.py:launch_session` is the sole production launch entry
point. It checks the session id, runtime name, provider and instance token,
commits a `starting` row, broadcasts `session.registered`, and only then calls
the provider. The production provider registry rejects calls outside this
authorized launch scope. Registration failure never starts a process.

| Caller | Executions it starts |
|---|---|
| `src/orchestrator/execution.py` | Task workers, reviewers, repairs, verifiers and playbook delegates |
| `src/orchestrator/pools.py` | Pool workers |
| `src/messages/session_lens.py` | Project and global named supervisors, including wake and restart |
| `src/agents/terminals.py` | Interactive worker terminals |

Hierarchical integration commits the starting row together with the branch
attachment before entering the same launcher. Its final acknowledgement
holds the branch ownership fence. Other launches retain an in-flight guard
so reconciliation cannot mistake the registration window for a dead process.
Launch acknowledgement releases only an unassigned agent reservation; it
preserves a task claimed during startup.

A failed partial launch is stopped and recorded in history. If stopping cannot
be confirmed, its registration and resources remain for reconciliation.
Cancellation follows the same cleanup protocol. A daemon crash can leave a
starting row without a process; normal adoption and exit classification recover
it. A process without a row is a legacy orphan or an invariant violation.

`tests/test_session_launch.py` enumerates every `.start` call, OS spawn and
tmux `new-session` site, and every caller of the launcher. A new launch path
fails this ratchet until it uses the registered path. Provider primitives can
be tested with explicitly injected registries. Human host shells strip AQ
identity markers and are not agent sessions; tool-free one-shot LLM calls,
account probes, service processes and validation jobs have separate lifecycles.

## Viewing and cleaning the flock

The dashboard's flock directory shows one row per starting, running,
draining or sleeping session. Its State filter narrows that list; **Show
stopped history** adds stopped and quarantined rows. Supervisors are listed
first, with global or project scope, role, launch provider/model/class, task,
state, uptime and last activity. Unknown settings on legacy rows stay unknown.
Registration events refresh the shared roster query before startup finishes;
the thirty-second poll remains a backstop.

`aq agent list` presents the same session table, followed by worker definitions.
`aq agent list --include-stopped` includes history. The `list_agents` API
returns `sessions` and `session_count` alongside the existing `agents` and
`count`; `project_id` filters sessions by their persisted scope, and
`include_stopped` enables history. Session visibility never depends on a
matching agent, current task assignment or claim epoch.

**Clean up** removes an inactive named session from the dashboard. Operators
can also use `aq session prune <session-id>`. Cleanup checks fresh provider
termination evidence and leftover processes carrying the instance marker,
revokes the old token, and conditionally deletes only that sleeping/stopped
named row with no task or claim. A live terminal, marked child process,
unavailable probe, changed instance or concurrent wake refuses cleanup.
Removing old supervisor history does not delete its worker definition or
stop a replacement session.

## Auditing the invariant

The reconciler audits the provider listings, process markers and flock
projection every thirty seconds. It reads registrations after process
observations so concurrent registered starts are not reported as orphans.
Process scans exclude sessions explicitly dialling another daemon on the
same host. Incomplete provider probes are findings, never a zero count.
Live processes associated only with stopped history are also errors.

Operators run `aq doctor --check sessions.untracked` for a focused check, or
`aq doctor --check stall.sweep` for the cross-project sweep. Violations
produce ERROR severity and a `flock_untracked` sweep line, including global
sessions when no project is active. Periodic findings are logged at ERROR.
After deployment, a healthy live install reports zero untracked sessions
and processes. Worker tokens cannot perform this operator verification;
workers record focused, area and disposable swarm evidence on their task.
