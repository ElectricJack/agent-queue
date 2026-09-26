# Discord supervisor conversations

An allowlisted operator can mention the bot user, for example `@Agent Q what
is blocking the build?`, in the configured channel to open a thread with the
global supervisor. This route is off by default. It deliberately relaxes the
2026-09-08 Discord simplification without restoring slash commands, task
controls, gate buttons, worker input or unrestricted channel chat.

## What enabling means

The approved mention-routing spec states:

> Choose transport-neutral intake commands plus durable conversation rows and the
> shared outbound-delivery implementation owned by the narrative spec. Opt-in extension;
> `discord.conversation.enabled=false` preserves notification-only behavior.
> Enabling requires a non-empty explicit user allowlist, configured guild/channel,
> `messages.enabled`, completed cutover and an enabled supervisor. Enabling means
> those gateway identities are trusted operator correspondents: their input reaches
> the **existing elevated global supervisor**. This is not a sandboxed chatbot.

The supervisor's default procedure is to answer, read and prepare proposals;
operational or bulk changes are proposed for dashboard action. Conversation
text itself cannot resolve gates, create approval evidence or call
`escalation_apply_reply`. Prompt instructions cannot hard-restrict the tools of
an already-running elevated session. An installation requiring enforced
read-only chat must keep this feature off until a separately scoped author
session exists. There is no per-project routing syntax in this version; the
supervisor clarifies project identity when needed.

## Enable the route

An operator edits `~/.agent-queue/config.yaml`, preserving the existing token,
digest and escalation settings. Use numeric Discord IDs for the guild, channel
and trusted users. This example opts in explicitly:

```yaml
discord:
  bot_token: "${DISCORD_BOT_TOKEN}"
  guild_id: "123456789012345678"
  channel_id: "234567890123456789"
  authorized_users: ["345678901234567890"]
  conversation:
    enabled: true
messages:
  enabled: true
sessions:
  enabled: true
```

Both the gateway and `supervisor_inbox_post` require all eight preconditions:

| Requirement | Code in `preconditions.unmet` when missing |
|---|---|
| `discord.conversation.enabled: true` | `conversation_disabled` |
| Non-empty explicit `discord.authorized_users` | `empty_allowlist` |
| Configured `discord.guild_id` | `no_guild` |
| Configured `discord.channel_id` | `no_channel` |
| `messages.enabled: true` | `messages_disabled` |
| `sessions.enabled: true` for supervisor delivery/cold start | `sessions_disabled` |
| Discord cutover status `complete` | `cutover_incomplete` |
| Conversation delivery outbox bound by the daemon | `outbox_unbound` |

An empty allowlist disables conversations. Enabling the flag with a missing
allowlist, guild or channel fails config validation. A bound outbox is runtime
wiring, not a setting to bypass. Digest and external escalation enablement are
independent of the conversation flag.

In the Discord Developer Portal, enable the bot's privileged **Message Content**
intent. Non-mention thread follow-ups depend on it. In the configured channel,
grant **View Channel**, **Send Messages**, **Create Public Threads**, **Read
Message History** and **Send Messages in Threads**. Missing intent or permissions
can prevent intake, thread creation or reconnect recovery; check diagnostics
rather than treating an empty response as success. The runtime intent diagnostic
reflects the bot's requested intent; also verify the Portal setting.

Use these read-only checks from an operator shell or the dashboard, after the
normal deployment/config reload procedure:

```bash
aq supervisor-inbox status
aq digest status
```

Status and history require a local operator or the elevated global supervisor;
ordinary worker and project-supervisor tokens are refused. Check `enabled`,
`preconditions.ok`, `preconditions.unmet`, `diagnostics.outbox_bound`,
`diagnostics.message_content_intent` and `diagnostics.permissions`. A null
intent/permissions field means the cached gateway information is unavailable,
not permission granted.

## What happens to a mention

Mention the actual bot user at the top level of the configured channel. A role
mention, `@everyone` or a typed/quoted mention token without a gateway-observed
bot mention cannot open a conversation. The bot mention is stripped from the
normalized input, which must contain some remaining text.

AQ persists the conversation, verified author and input before queuing any
Discord delivery. It queues one thread-open acknowledgement on the operator's
root message and sends a durable input notice to `supervisor-global`. Follow-up
messages in the bound thread need no new mention; each is checked against the
current allowlist and conversation audience.

Only an explicit `supervisor_inbox_reply` from the assigned global supervisor
or local operator queues an answer. Generic mailbox messages, `message.sent`
events and transcript-tail fallback replies are never relayed. The Discord
reply is at most 1,900 characters including its marker and dashboard pointer;
a longer answer stays in conversation content with a dashboard pointer. Sends
disable all mentions, remove control characters and mention tokens, and retain
only links beginning with the configured dashboard base URL.

If thread creation fails, AQ queues one bounded failure reply in the channel
and marks delivery blocked; the accepted input remains visible internally.
It does not start inline channel chat. If an input remains unanswered after
15 minutes, one deduplicated delay notice says it is still queued. An
acknowledgement or delay notice is not completion. Removing an author from the
allowlist revokes future intake and pending replies that would expose content
to the newly restricted audience.

The durable outbox leases work, prioritizes escalations and reconciles stable
markers after an ambiguous send. Unknown delivery is not proof of failure or
success; it must be reconciled before resending.

## Fixed limits

These values are shared code constants, not operator tuning settings.

| Bound | Value |
|---|---|
| Normalized input length | 4,000 Unicode code points |
| Accepted inputs per author | 10 per sliding 10 minutes |
| Accepted inputs per channel | 60 per sliding 10 minutes |
| Rate-limit notice | At most one per author per 10-minute bucket |
| Outbound reply, including pointer and marker | 1,900 characters |
| Reconnect history | Last 24 hours, at most 1,000 messages per pass |
| Unanswered-input delay notice | After 15 minutes |
| Conversation text retention | 30 days |
| Dedup tombstone retention | 90 days |

Replay of an already accepted external message does not spend quota again.
Oversize inputs and rate-limited requests receive bounded, deduplicated notices.

## What is ignored and how to see it

Escalation intake runs first. A message in a known escalation thread stays
exclusively with that intake, even if it refuses or fails; it cannot fall through
into a supervisor conversation. See the [escalation intake code table](escalations.md#check-that-it-is-working).

Conversation classification uses these stable ignore codes:

| Code | Meaning |
|---|---|
| `preconditions_unmet` | At least one of the eight prerequisites is missing; inspect status |
| `own_message` | The bot's own post |
| `bot_author` | Another bot's post |
| `webhook_author` | A webhook post |
| `dm` | A direct message |
| `edit` | An edit, which cannot rewrite accepted instructions; send a new message |
| `foreign_guild` | Outside the configured guild |
| `foreign_channel` | Outside the configured channel or its threads |
| `author_not_allowlisted` | Author absent from the current allowlist |
| `no_bot_mention` | Top-level message lacks a real bot-user mention |
| `escalation_thread` | Thread belongs exclusively to an escalation |
| `unknown_thread` | No matching conversation binding |
| `empty_text` | No text remains after normalization |
| `classify_error` | Observation/classification failed; inspect the warning traceback |

Ignored messages are silent. Each router ignore logs one INFO line,
`discord intake ignored reason=<code> guild=<id> channel=<id> message=<id> author=<id>`,
with IDs and code only, never discarded content. `digest_status.intake` and
`supervisor_inbox_status.intake` expose the same bounded last-hour counter:
`window_seconds`, `total`, `ignored` and `available`. It resets on daemon restart
and holds at most 10,000 events. Escalation classification can record a refusal
before the conversation router accepts that message, so an escalation ignore
code alone does not prove a mention was dropped. Command refusals instead log
`discord conversation refused reason=<code>`; inspect that line for
`oversize`, `rate_limited`, `conversation_closed` or `post_failed`. The first
three produce bounded notices rather than silent ignores.

Gateway filters still discard its own posts, authors outside a non-empty
allowlist and messages arriving before cutover completes, before router
diagnostics. Edits are not ingested as replacement inputs.

## Reconnect backfill and gaps

On gateway READY or RESUMED, AQ reads bounded history in the configured
channel and bound active conversation threads. Durable per-channel cursors
advance only after each page is persisted. External-message deduplication
makes overlap with live gateway intake safe.

History older than the 24-hour horizon, unavailable history and passes cut
short by the 1,000-message cap record `intake_gap` entries. Read
`supervisor_inbox_status.backfill.cursors` and `backfill.gaps`; a gap means
messages may have been missed, not delivered. Correct permissions/intent and
send a new explicit message when needed. AQ never claims that inaccessible
history was recovered.

## Retention and closed threads

Maintenance expires stored input text after 30 days and archives associated
internal conversation messages. History marks expired text with
`text_expired`. Dedup tombstones survive for 90 days so old external inputs
cannot replay as fresh work during that period. Idle conversations close
after 30 days; closing or archiving an external thread does not delete the
durable records or authorize reopening work. A follow-up to a closed
conversation receives a bounded notice asking for a new top-level mention.
These retention rules concern AQ records; they do not delete Discord history.

```bash
aq supervisor-inbox history
```

History is paged, with `next_before` cursors. Dashboard supervisor history shows
verified Discord sender identity and supports filtering by conversation.

## Related implementation and guidance

Read [Messaging](../concepts/messaging.md), [Escalations and the hourly
digest](escalations.md) and the [replacement checklist](discord-replacement-checklist.md).
The approved design is in the operator vault at
`projects/agent-queue/specs/2026-09-24-discord-mention-routing-to-the-supervisor.md`;
the [simplification spec](../superpowers/specs/2026-09-08-discord-simplification-implementation.md#1-decisions-and-scope)
records its deliberate opt-in exception.

Intake and bounds live in [`src/conversations/`](../../src/conversations/),
gateway order in [`src/discord/inbound.py`](../../src/discord/inbound.py), and
command identity/state changes in
[`src/commands/conversation_commands.py`](../../src/commands/conversation_commands.py).
Documentation is checked by `tests/test_discord_docs.py` and command examples
by `tests/test_guidance_docs.py`.
