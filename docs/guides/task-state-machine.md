---
tags: [tasks, state-machine, lifecycle]
---

# Task state machine — moved

<!-- aq:redirect -->
> **This page moved.** The current explanation of task states and transitions is
> [Tasks, epics, dependencies and task graphs](../concepts/tasks.md). This stub
> is kept so existing links resolve.

The old table predated the status collapse, the claim protocol and the typed
dependency edges, so it listed states that no longer exist. Look up an enum
value with `aq schema` rather than trusting a documented list.

| You were looking for | Read |
|---|---|
| States, transitions and what moves a task | [Tasks](../concepts/tasks.md) |
| Why a task is not running | `aq task explain <id>` |
| The authoritative enum values | `aq schema` |
| Who picks a task up and when | [Scheduling, worker pools and resource limits](../concepts/scheduling.md) |
| The original design spec | [Models and state machine](../specs/models-and-state-machine.md) (historical) |
