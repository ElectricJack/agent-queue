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
escalation. There are no AQ slash commands, task controls, gate buttons,
worker-input routing or streamed execution threads. Replies inside an escalation
thread reach the owning project's supervisor. The explicit opt-in exception is
[Discord supervisor conversations](discord-conversations.md): a bot mention by
an allowlisted operator can open a thread with the elevated global supervisor.
This deliberate trust decision does not restore the removed controls.

Operator control lives in the dashboard Command Center and the `aq` CLI.

| You were looking for | Read |
|---|---|
| What AQ posts, and when it stays quiet | [Messaging](../concepts/messaging.md) |
| Configuring the channel, digest and mentions | [Escalations and the hourly digest](escalations.md) |
| Opting into bot-mention conversations | [Discord supervisor conversations](discord-conversations.md) |
| Approving something | The dashboard Gates drawer, or `aq task gate-resolve` |
| The cutover that removed the old controls | [Discord migration](discord-migration.md) (historical) |
