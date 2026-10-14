# Stuck-task watch

The optional system `stuck-task-watch` V2 playbook runs on `timer.15m`. It uses
reusable snapshot, collection and notification-state primitives. A complete
active-project inventory and pre-scan notification-state manifest precede every
diagnostic scan. Findings go to `session:supervisor-global` through atomic
`playbook_state_notify`; the playbook never repairs work.

The watch observes exactly the five finding kinds in `task_observations_v1`,
all BLOCKED tasks, and `get_stuck_tasks` rows whose actual state is ASSIGNED
with age greater than 1,800 seconds or IN_PROGRESS with age greater than 7,200
seconds. Aged ORPHANED rows qualify; younger ORPHANED and all READY rows do not.
Producer exclusions below still apply to the stuck-task source.

Each project/source/task identity has an independent notification episode.
Doctor findings retain their producer key. Repeated observations update evidence
without another message; a complete scan clears absent keys using the saved
pre-scan revisions. Incomplete partitions and failed page traversals cannot clear
their episodes. Conflicts defer to the next tick without refreshing the revision.
Inactive projects retain state without notifications or clears.

The base policy has no reminder interval. The separately reviewed
`stuck-task-watch-reminder` fixture repeats after 86,400 seconds and has its own
namespace. Namespace schema version 1 survives recompilation, restart and
disable/re-enable. Supervisors choose the next action. The previous stateless
ten-minute policy remains a legacy test recording.

## Detection

The command observes only ACTIVE projects whose scheduling is not paused.
An optional project filter narrows the read. Scoped principals cannot read
another project's tasks. Containers are excluded using declared metadata or
actual child links, including archived children. Open human gates, `hold:*`
labels, manual holds and active operator hold decisions suppress findings;
ancestor holds also suppress descendants.

Existing configurable time-in-state checks remain: ASSIGNED for more than
30 minutes or IN_PROGRESS for more than two hours. Additional checks are:

* `ORPHANED`: an ASSIGNED/IN_PROGRESS leaf older than 20 minutes whose
  assigned worker is not busy with that task, lacks a live session for the
  current claim, or has a task heartbeat or session activity older than
  20 minutes. Session liveness uses the shared agent-liveness helpers.
* `STUCK READY`: a READY leaf older than 30 minutes whose canonical task
  explanation contains `route_no_candidates` or `frontier_route_not_claimable`.
  Ordinary dependency and capacity waits do not trigger this check.

A task appears once, with the orphan reason preferred over the existing age
threshold. Failed explanation reads fail the tick instead of sending a partial
list. The command does no writes; messaging is a separate playbook capability.
No local operator script, Docker container, checkout path or task-ID allowlist
is needed.

## Installation

Ship the source, deterministic reviewed artifact, manifest and fixture together.
Neither watch is a first-install default or a daemon readiness requirement.
Operators explicitly import and activate the intended reviewed bundle; a restart
never replaces operator policy. See the [rollout guide](../guides/playbook-building-blocks.md).
