# Reroute stopped tasks after a provider outage

Task: `prime-falcon-56`.

`aq task route` already accepts unassigned BLOCKED tasks, but stopping a task
can leave its session live until the reconciler runs. The database routing
guard correctly refuses that session. A successful stop must stop and confirm
the exact session instance before releasing its task assignment and workspace.
If termination fails or cannot be proved, retain those resources and report
the refusal. Keep integration writer handoff checks before workspace release.

Provider failover sweeps must also consider unassigned BLOCKED tasks on an
unavailable provider. Exclude live sessions in candidate enumeration and retain
the atomic write guard. A move changes routing only: BLOCKED status, terminal
stop metadata, retry counts and graph blockers survive. Pins, class policy,
recorded router candidates and capacity limits still apply. An explicit restart
is required to run stopped work.

A dry run is an observational snapshot and must not wait for an applying sweep's
mutation lock. Applying sweeps remain serialized and use guarded writes. The CLI
provider reroute command gets a 120-second read budget for fleet-wide reads,
including dry runs; it never automatically replays a lost mutation response.

Acceptance: stop then route succeeds without waiting for reconciliation; failed
termination preserves ownership; blocked failover is previewed and applied
without waking the task; live sessions are excluded and concurrent claims still
win; a preview completes while the applying lock is held. Verify focused routing,
failover, session stop and CLI transport tests, related area checks and the
disposable swarm end-to-end smoke run.
