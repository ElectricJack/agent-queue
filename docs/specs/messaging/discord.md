---
tags: [spec, messaging, discord, bot]
---

# Discord adapter specification

<!-- aq:historical -->
> **Design record — not current documentation.** A spec states the behaviour
> intended when it was approved; it is written before the code and is not
> revised to track it. Where this page and the code disagree, the code is right.
> Start at [the documentation home](../../README.md) for what AQ does today, and
> see [historical material](../../history/README.md) for how this material is
> organised.

The Discord adapter is a one-channel external notification surface. Core state,
authorization, escalation ownership, reply persistence, digest eligibility and
recovery decisions remain transport-neutral.

## Outbound behavior

- `DiscordEscalationTransport` delivers durable escalation roots, threads,
  acknowledgements, correlated supervisor follow-ups, and terminal outcomes.
- The digest scheduler posts at most one eligible installation-wide digest per
  configured window.
- Both use the explicit numeric `discord.channel_id`, durable leases/receipts,
  bounded retry handling, and the shared rate guard. Neither creates channels.
- Historical posts, channels and task threads are never deleted automatically.

## Inbound behavior

`AgentQueueBot.on_message` passes messages only to
`DiscordEscalationIntake`. The intake accepts an authorized human reply only
when its observed channel/thread pair matches the durable escalation binding.
It invokes `escalation_reply`; it has no task, gate, playbook, project or worker
mutation path. Channel chatter, DMs, mentions, old task-thread replies and
unrelated threads are inert.

## Startup cutover

Before inbound routing is enabled, `run_discord_cutover` inventories pending
human questions and gates, clears bot-owned legacy views, and migrates each
conversation to a stable escalation source identity. Repeating the pass after
a crash or restart is idempotent. A compatible old root in the selected shared
channel is adopted instead of reposted. Already accepted answers are stored as
immutable legacy evidence, resolved without supervisor redelivery, and never
create new delivery work.

One unambiguous legacy destination name may be resolved to an ID in memory.
Conflicting names never select a destination. Migration status appears in
`digest_status` and daemon health.

## Removed surfaces

The bot registers no Agent Queue slash commands and removes only the six former
names (`status`, `tasks`, `explain`, `peek`, `gates`, `attach`) during command
sync. General chat, immediate lifecycle notifications, task/action views,
question modals, execution streaming, per-project channel provisioning, and
task-thread cleanup are removed. Shared domain events and core recovery
commands remain available to dashboard, CLI and plugin consumers.
