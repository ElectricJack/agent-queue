---
tags: [discord, interface]
---

# Discord interface — moved

<!-- aq:redirect -->
> **This page moved.** Read
> [Messaging, digests and escalations](../concepts/messaging.md) for the model
> and [Escalations and the hourly digest](escalations.md) for the operator
> procedure. This stub is kept so existing links resolve.

**Discord is notification-only by default.** AQ posts an activity
digest to one configured channel and opens one thread per durable human
escalation. The six legacy AQ slash commands remain retired, along with gate buttons,
worker-input routing or streamed execution threads. Replies inside an escalation
thread reach the owning project's supervisor. The explicit opt-in exception is
[Discord supervisor conversations](discord-conversations.md): a bot mention by
an allowlisted operator can open a thread with the elevated global supervisor.
This deliberate trust decision does not restore the removed controls.

The explicit task deletion action is `/delete-task task_id:<id> reason:<optional>`.
An allowlisted operator or administrator must invoke it in the configured AQ channel.
Its private confirmation lets you choose this task only or all descendants, and
keep or delete local/remote branches and worktrees. It previews exact resources,
stops selected running work and records a durable audit. Cleanup failures are warnings.
No other retired task controls return; browsing and other controls live in the
Command Center and the `aq` CLI.

| You were looking for | Read |
|---|---|
| What AQ posts, and when it stays quiet | [Messaging](../concepts/messaging.md) |
| Configuring the channel, digest and mentions | [Escalations and the hourly digest](escalations.md) |
| Opting into bot-mention conversations | [Discord supervisor conversations](discord-conversations.md) |
| Approving something | The dashboard Gates drawer, or `aq task gate-resolve` |
| The cutover that removed the old controls | [Discord migration](discord-migration.md) (historical) |
