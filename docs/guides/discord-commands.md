---
tags: [discord, interface]
---

# Discord interface

Agent Queue's Discord integration is deliberately notification-only. It posts
eligible hourly activity digests to one configured channel and opens one thread
for each durable human escalation. There are no Agent Queue slash commands,
general bot chat, task controls, gate buttons, worker-input routing, or streamed
execution threads.

Use the dashboard Command Center for task browsing and controls, the Gates
drawer for approvals, session detail for logs and terminal access, and the
dashboard supervisor chat for general conversation. The CLI provides the same
core operations for scripted use.

## Replying to an escalation

Reply inside the escalation's own thread. Replies from users in
`discord.authorized_users` are persisted with their Discord identity and sent
to the owning project supervisor. The reply itself never changes a task,
resolves a gate, or sends input directly to a worker. The supervisor reloads
the current state and applies any authorized recovery through the normal core
command.

Messages in the shared channel, direct messages, arbitrary mentions, old task
threads, and unrelated threads are ignored for work routing. Replies to closed
escalations receive closed-state guidance and cannot reopen work.

## Configuration

Configure one numeric `discord.channel_id`. Digest and escalation delivery can
be enabled independently. Legacy channel names are read only during the
one-way startup migration; conflicting old destinations require an explicit
channel selection. Agent Queue never creates or deletes channels during the
cutover.

See [Discord replacement capability checklist](discord-replacement-checklist.md)
for the dashboard/CLI replacement for each retired surface.
