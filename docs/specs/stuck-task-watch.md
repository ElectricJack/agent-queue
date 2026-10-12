# Stuck-task watch

The system `stuck-task-watch` V2 playbook runs on `timer.10m`. It calls the
read-only `get_stuck_tasks` command. An empty list ends the run; a nonempty
list is sent through `message_send` to `session:supervisor-global` as one
summary with one line per task: project, task ID, state, elapsed minutes,
reason and suggested diagnostic command.

Every tick sends again while tasks remain stuck. There is no watch memory,
deduplication, six-hour reminder window or cleared notification. The playbook
never repairs work. Supervisors choose the next action.

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
It is a first-install default, not a daemon readiness requirement. Existing
installations explicitly import and activate the shipped system bundle; a
restart never replaces operator policy.
