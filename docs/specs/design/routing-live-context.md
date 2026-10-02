# Live context for delegation

`task_route_plan` extends the mandatory router's existing `Snapshot` with
`live_context` and `live_summary`. These describe compatible worker profiles
and providers without changing policy, provider choice, fleet limits, gates,
or claim admission. The classifier's existing task-only prompt remains unchanged.

The versioned context includes collection start/end and duration, provenance,
provider state/reason code/update age, and each observed quota window's scope,
percentage, observation/confirmation timestamps, reset and freshness. Missing
quota is unknown; stale or reset windows are retained as labelled observations
but do not become fresh scoring inputs. Weekly and five-hour windows remain
separate. Collector provenance is restricted to known probe/transcript labels.

Each compatible profile includes its enabled flag, class list, configured
effective limit, fleet busy/idle/starting/draining/unresponsive counts, project
supply, routed backlog, local idle claim capacity and launch/effective headroom.
Starting sessions and pending launches consume capacity. Preparing claims are
busy. Task workers' agent reservations also consume capacity before their
durable sessions exist, and are counted once when a session appears.
Draining, disabled and unresponsive workers are not idle claim capacity.
Pool idle capacity is local to the project because tokens/workspaces are scoped.

Launch headroom is bounded by the profile limit minus all live supply, the
remaining project worker cap, the global pool cap, acquirable workspace capacity
(including lazy worktree slots), and launch quarantine. Pending launches without
acquired workspaces conservatively reduce workspace observations. Existing local
idle workers can still claim when new launches have no workspace/cap headroom.
Effective headroom subtracts routed backlog from idle plus launch capacity.
Shared project, workspace and fleet headroom must never be summed across
profiles. These are observational upper bounds, not reservations or a promise
of immediate admission; identity, delivery and human gates remain authoritative.
Zero headroom permits routing queued work and does not create another scheduler.

The active-work summary contains task-kind counts only. Output is capped at 64
profiles, 16 providers, eight quota windows per provider, 32 classes per profile,
and the finite task-kind vocabulary; omission is explicit. The readable summary
is capped at 2400 characters. No account labels, task descriptions, transcripts,
session credentials, workspace paths or captured launch errors are included.
Quarantine reports `launch_backoff` and expiry, rather than captured stderr.

`task_route_apply` refreshes eligibility and load inside the existing fleet
advisory-lock transaction, checks provider/harness/lifecycle/class compatibility,
and reselects on current routed backlog. It stores the bounded apply observation
in the route record. Context supplied by a caller is never trusted for eligibility
or admission. Concurrent decisions observe previously committed routes; claim
and guarded route writes continue to protect held tasks.

Verification lives in `tests/test_routing_router.py` and
`tests/test_routing_planner.py`: supply states, stale/missing quota, provider
degradation/disablement, caps/workspaces/quarantine, pending launch accounting,
bounded/private output and concurrent apply freshness.
