---
tags: [overview, index]
---

# Agent Queue — documentation moved

<!-- aq:redirect -->
> **This page moved.** The documentation home is
> [`docs/README.md`](README.md); the project overview is the
> [repository README](../README.md). This stub is kept so existing links
> resolve, and it keeps the screenshots below reachable.

This page was a second landing page with its own feature list, and it drifted:
it described **SQLite-backed state** and "PostgreSQL supported for production
deployments". PostgreSQL is the only backend — there are no dialect branches
left in the code, and [`tests/test_sqlite_removal.py`](../tests/test_sqlite_removal.py)
fails if any come back. It also described a Supervisor LLM as the way you talk
to AQ, which is no longer how the operator surfaces work.

## Where to go instead

| You were looking for | Read |
|---|---|
| What AQ is and why it exists | [Repository README](../README.md) |
| Where all the documentation is | [Documentation home](README.md) |
| Installing it and running one task | [Install](tutorials/install.md) → [Your first task](tutorials/first-task.md) |
| How the daemon is put together | [System architecture](concepts/architecture.md) |
| The vocabulary | [Glossary](reference/glossary.md) |
| Design specs and old plans | [Historical material](history/README.md) |

## Screenshots

Retained here so the landing pages can reuse them.

<table>
<tr>
<td><img src="img/project-chat-00.png" alt="Digest and escalation activity in the configured Discord channel" width="450"></td>
<td><img src="img/project-chat-01.png" alt="A task started and completed — token usage and change summary" width="450"></td>
</tr>
</table>

![An agent at work — reading code, fixing bugs, running tests, committing](img/task-thread.png)

![System status and task tree — agents, progress, queued work at a glance](img/system-status-task-list.png)

![Task information — status, assignment, dependencies](img/task-information.png)
