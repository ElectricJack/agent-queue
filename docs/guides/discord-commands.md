---
tags: [discord, interface]
---

# Discord interface — moved

<!-- aq:redirect -->
> **This page moved.** Read
> [Messaging, digests and escalations](../concepts/messaging.md) for the model
> and [Escalations and the hourly digest](escalations.md) for the operator
> procedure. This stub is kept so existing links resolve.

Discord carries digests, reports and durable human asks in one configured
channel. Legacy escalations retain their threads until destination ownership
transfers. The six former AQ slash commands, general task controls, worker-input
routing and streamed execution threads remain retired. Canonical ask and legacy
escalation choice buttons are supported decision surfaces.

Opt-in [supervisor conversations](discord-conversations.md) reply in the channel
and route to the project supervisor, falling back to global. A bot mention is
required by default; `require_mention: false` permits plain channel messages and
`allow_dm: true` permits allowlisted DMs. Existing bound threads remain supported.
Ask channel mode exclusively owns inbound input before these routes; inspect
`aq ask cutover-status` and [the rollout guide](human-asks-rollout.md).

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
| Opting into supervisor conversations | [Discord supervisor conversations](discord-conversations.md) |
| Approving something | Dashboard Needs you and canonical ask decisions; legacy gate CLI where applicable |
| The cutover that removed the old controls | [Discord migration](discord-migration.md) (historical) |
