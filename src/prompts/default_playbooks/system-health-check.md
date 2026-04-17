---
id: system-health-check
triggers:
  - timer.30m
scope: system
---

# System Health Check

Every 30 minutes, check overall system health and surface problems
that need attention. Focus on conditions that block progress or
indicate something is silently broken.

## Stuck tasks

Look for tasks in ASSIGNED or IN_PROGRESS status that have been
in that state for an unusually long time (more than 30 minutes for
ASSIGNED, more than 2 hours for IN_PROGRESS). These may indicate
an agent crash, a rate-limit stall, or a task that is spinning
without making progress.

If a stuck task is found, check whether the assigned agent is still
alive. If the agent is running but idle, restart the task. If the
agent is dead, reassign the task to another available agent or move
it back to PENDING.

## Blocked tasks

Check for BLOCKED tasks that have no resolution path — no upstream
dependency that would unblock them, no human input requested. These
tasks are stuck forever unless someone intervenes. Flag them for
review.

Also check for circular dependency chains where two or more tasks
block each other.

## Agent health

Verify that all agents marked as active are actually responsive.
Check for agents that have been in an error state or have not
picked up work despite being idle with tasks available.

## Summary

If any issues are found, post a concise summary to the project
channel. Group findings by severity: critical (stuck tasks blocking
the queue), warning (blocked tasks with no path), info (minor
anomalies). Skip the summary entirely if everything looks healthy
— do not post "all clear" messages.

Save findings to system memory so that other playbooks (like the
codebase inspector) can check for related issues before creating
duplicate tasks.
