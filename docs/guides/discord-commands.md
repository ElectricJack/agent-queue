---
tags: [discord, interface]
---

# Discord interface — moved

<!-- aq:redirect -->
> **This page moved.** Read
> [Messaging, digests and escalations](../concepts/messaging.md) for the model
> and [Escalations and the hourly digest](escalations.md) for the operator
> procedure. This stub is kept so existing links resolve.

What this page said remains true, and is worth repeating because older pages
still imply otherwise: **Discord is notification-only.** AQ posts an activity
digest to one configured channel and opens one thread per durable human
escalation. There are no AQ slash commands, task controls, gate buttons,
worker-input routing or streamed execution threads. Replies inside an escalation
thread are the only inbound path, and they reach the owning project's
supervisor.

Operator control lives in the dashboard Command Center and the `aq` CLI.

| You were looking for | Read |
|---|---|
| What AQ posts, and when it stays quiet | [Messaging](../concepts/messaging.md) |
| Configuring the channel, digest and mentions | [Escalations and the hourly digest](escalations.md) |
| Approving something | The dashboard Gates drawer, or `aq task gate-resolve` |
| The cutover that removed the old controls | [Discord migration](discord-migration.md) (historical) |
