# aq task lifecycle administration — obsolete work, stale checks, archives, holds

Reference for [the aq-tasks skill](../SKILL.md). These actions belong to an
operator or a supervisor; a worker session is refused them and should know
only what they do, so it can name the right request in a comment or message.

## Contents

- Superseded and stale work
- Archives
- Recovery holds

## Superseded and stale work

Work that is no longer needed (a duplicate fix landed another way, or the plan
changed) is retired by an operator or supervisor with one command, not with a
failing close plus a status edit:

```bash
aq task close <id> --obsolete --reason "superseded by PR #639"
```

It moves the task to COMPLETED as abandoned without publishing anything, so its
dependents stop waiting. It releases the task's branch owners through the
release-owner safety proof and cancels any parked development batch that lists
it. A batch still publishing, an open development repair or a refused owner
proof stays pending in the task's `obsolete` metadata, and the daemon retries it.
Once nothing is pending the task can be deleted. A worker session is refused:
close your own task with `--outcome` and name the superseding work in the summary.

The daemon checks for stranded lifecycle state on its own:

- A container stuck BLOCKED or PAUSED is completed as soon as every child is
  COMPLETED and delivered, and again on every daemon start.
- A task left BLOCKED or PAUSED past `work_graph.stale_open_after_seconds`
  (default 6h) is re-checked. If its blocker is gone, it is unblocked. If not,
  it gets `needs_attention=stale_open` and the supervisor gets one message. The
  flag is advisory: it clears itself once the task leaves that status.
- `aq doctor --check tasks.dangling_lifecycle` lists finished work that still
  holds branch owners or batch membership, and open containers whose children
  are all done.

## Archives

Completed / failed tasks eventually archive:

```bash
aq task list-archived --project-id <pid>   # what has been archived
aq task archive --task-id <task_id>        # archive one COMPLETED/FAILED/BLOCKED task
```

Archiving is one-way: there is no restore or unarchive command, and an
archived id can never be recreated in a different project.

## Recovery holds

Recovery holds require the exact incident id and prior `decided_at` timestamp.
Only a local operator or live global supervisor can run `aq task recover
--operator-release --decision release --task-id TASK --incident-id INCIDENT
--expected-hold-at DECIDED_AT --reason WHY`. This lifts only the hold, retaining
task state, counters and integration ownership. For ordinary BLOCKED tasks they
may choose `--decision retry` to authorize a reviewed nonautomatic failure while
retaining fresh stopped-worker proof, routing, gate, resource and budget guards.
Project supervisors must request that authority. Supervisor-authored train repair
and `slot_reset_failed` holds escalate after 15 minutes; execution remains held
until audited release. Explicit human holds never expire. Never delete metadata
or reset counters to release a hold.

