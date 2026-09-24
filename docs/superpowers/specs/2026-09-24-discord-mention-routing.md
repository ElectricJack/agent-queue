# Discord @mention routing to the supervisor — preliminary spec

**Date:** 2026-09-24 · **Status:** preliminary — analysis and options, no decision taken
**Roadmap:** [operator feedback roadmap](2026-09-24-operator-feedback-roadmap.md)
**Related:** [voice transcripts](2026-09-24-discord-voice-transcripts.md) (builds on this),
[Tailscale dashboard link](2026-09-24-tailscale-dashboard-link.md),
[supervisor narrative updates](2026-09-24-supervisor-narrative-updates.md),
[morning report playbook](2026-09-24-morning-report-playbook.md),
[Discord simplification](2026-09-08-discord-simplification-implementation.md),
[projectless supervisor](2026-08-30-projectless-supervisor-design.md),
`docs/guides/escalations.md`, `docs/guides/discord-replacement-checklist.md`

## 1. The ask

> Inbound: @Agent Q mentions route to the supervisor (currently don't). Small, and it's the
> operator's only remote feedback channel.

The operator wants to type `@Agent Q <something>` in the configured Discord channel, from a
phone, and have the (global) supervisor receive it and answer. Today the dashboard supervisor
chat (`/agents`) is the only way to talk to the supervisor, and the dashboard is effectively
not reachable remotely (see the [Tailscale link bug](2026-09-24-tailscale-dashboard-link.md)).

**This is a deliberate relaxation of a security/product decision.** The simplification spec
says Discord has "no operational slash commands, execution streaming, **arbitrary mention
handling or general chatbot mode**" (`2026-09-08-discord-simplification-implementation.md`
§1 item 4, and the removal table: "General channel chat, arbitrary mentions and DMs as
commands → Ignore for work routing → Dashboard supervisor chat"). The replacement checklist
marks "General channel chat / mention routing → Dashboard supervisor chat (`/agents`) —
Ready". The spec must therefore re-justify the route and keep every invariant it can.

## 2. What exists today

### 2.1 Exact trace of an `@Agent Q hello` in the configured channel

1. `AgentQueueBot.__init__` (`src/discord/bot.py:30-32`) requests `Intents.default()` plus
   `message_content = True` (a **privileged** intent; if it is not enabled in the Developer
   Portal, login raises `PrivilegedIntentsRequired`, which `src/main.py` `run_bot` treats as a
   login failure and runs the daemon with messaging disabled).
2. `on_message` (`bot.py:155-166`): drops the bot's own messages; drops authors failing
   `_is_authorized` (`bot.py:53-55`) — **note this is fail-open: an empty
   `discord.authorized_users` authorises everyone at this layer**; drops everything until the
   cutover (`src/discord/cutover.py`) reports `complete`; then calls
   `DiscordEscalationIntake.handle(message, bot_user_id=...)`. The return value is ignored.
3. `DiscordEscalationIntake.observe` (`src/discord/escalation_intake.py:51-68`) builds an
   `InboundMessage`; for a top-level channel message `parent_id` is absent, so
   `thread_id=None` and `channel_id` is the channel itself. `message.mentions`,
   attachments and `message.reference` are not read at all.
4. `classify` (`escalation_intake.py:70-97`) runs `classify_inbound`
   (`src/escalations/intake.py:83-150`) with `binding=None`. Gate order: disabled →
   own message → bot author → **`"message is not in a thread"`** (`intake.py:107-110`,
   the comment names "a mention outside a thread" explicitly). Result `ACTION_IGNORE`.
5. `handle` (`escalation_intake.py:113-114`) returns `False`. **Nothing is logged** — the
   `IntakeDecision.reason` string exists "for the operator log" (`intake.py` docstring) but
   `handle` never logs an ignore. No reaction, no reply, no DB row, no event.

So the mention is **silently dropped with no trace**. Variants:

| Message | Outcome today |
|---|---|
| `@Agent Q …` top-level in configured channel | ignored, "message is not in a thread", no log |
| same, `discord.escalation.enabled: false` | ignored earlier, "escalation intake is disabled" |
| `@Agent Q …` inside an **escalation** thread, allowlisted author | accepted as an escalation reply; the raw `<@botid>` token is kept in the text |
| `@Agent Q …` in any other thread | one `find_escalation_by_thread` query, then ignored |
| DM to the bot | no `parent_id` → "not in a thread" → ignored |
| message edits | no `on_message_edit` handler; never seen |
| author not on allowlist, allowlist empty | passes `bot._is_authorized`, refused by `classify_inbound` (`intake.py:115-116`) |

Tests pin this: `tests/test_escalation_intake.py::test_chatter_mentions_dms_spoofed_identities_and_bot_loops_never_create_work`,
`tests/test_discord_bot_routing.py`, `tests/test_discord_simplification_lifecycle.py`.

### 2.2 The escalation reply path (the one inbound route, and the template)

- Identity: gateway author only, checked against `discord.authorized_users`, handed to the
  core as `ExecutionPrincipal.service(f"discord:{author_id}")`
  (`escalation_intake.py:116`; `src/commands/principal.py:136-138` — `policy=DENY_ALL`).
  `_verified_reply_identity` (`src/commands/escalation_commands.py:296-306`) maps it to
  `human:discord:<id>`; `_reject_authority_args` refuses body-supplied identity.
- Persistence: `accept_escalation_reply` (`src/database/queries/escalation_queries.py:304-455`)
  inserts the `messages` row to the supervisor (`body_kind="escalation_reply"`,
  `to_kind="session"`) and the `escalation_messages` row in one transaction; replay is
  idempotent on `(transport, external_message_id)`.
- Outbound: `EscalationDeliveryService` (`src/escalations/dispatch.py`) plans
  ROOT/ACK/RELAY/RESOLUTION deliveries (`src/escalations/plan.py:163-250`) into the
  `escalation_deliveries` outbox (unique `dedup_key`, lease, receipts, `aq-esc:` marker
  reconciliation of ambiguous sends). `DiscordEscalationTransport`
  (`src/discord/escalation_transport.py`) exposes `post_root`, `ensure_thread` (creates a
  thread on an existing message by ID — reusable on the operator's own message),
  `post_thread_message` (always `AllowedMentions.none()`), `edit_root`, `archive_thread`.
- Gap even here: **no command lets the supervisor post a free-text message into an escalation
  thread**. RELAY deliveries exist, but outbound `escalation_messages` rows are written only by
  the action-failure path (`escalation_commands.py:550`) and the cutover (`cutover.py:186`).

### 2.3 Supervisor messaging

- Address `supervisor-global`, projectless (`project_id = NULL`) per the projectless design.
  `message_send` to `session:supervisor-global` is a *system message* and requires local or
  elevated-global scope (`src/api/scope.py:192-200`,
  `MessageCommandsMixin._system_message_scope_error`, `src/commands/message_commands.py:85-91`).
  How a `SERVICE` principal maps onto `_current_scope` for this check is **unverified**.
- The dashboard chat posts `POST /api/sessions/<address>/message` with `from_kind: "user"`,
  `from: "dashboard"` (`dashboard/src/api/chat.ts:71-91`).
- `MessageDeliveryEngine` (`src/messages/delivery.py`) wakes a sleeping supervisor
  (`SessionLens.ensure_started` only for the `supervisor-` prefix), nudges an idle one,
  skips a busy one. Supervisor answers come back as `message_reply` rows addressed to the
  original sender (`message_commands.py:284-369`); `to_kind="user"` rows are marked
  delivered `via="platform"` and emit `message.sent` (`delivery.py:265-271`). The
  transcript-tail fallback fabricates a reply from the supervisor's last assistant turn
  when none was written (`delivery.py` ~200-262). **Nothing in `src/discord/` subscribes to
  `message.sent` any more** (the comment at `delivery.py` naming Discord's `_on_message_sent`
  is stale), and `message.sent` is a bus hint, not a durable outbox.
- Prior art for deterministic inbound routing: the `aq-inbox` internal plugin
  (`src/plugins/internal/inbox/plugin.py`) classifies mail with no LLM and emits
  `email.received.allowlisted` / `email.received.unknown` for playbooks to consume.

## 3. Gaps

1. No inbound route for a mention (or any non-escalation message) — by design today.
2. No outbound path from a supervisor reply to Discord outside the escalation state machine.
3. No conversation identity binding a Discord message/thread to a `messages.thread_id`.
4. `escalations.project_id` is `NOT NULL` with a FK to `projects` (`src/database/tables.py:1333`)
   and every row needs `summary`/`investigation`/`decision_requested` — the escalation table
   cannot hold a projectless, human-initiated chat without a schema change.
5. No inbound rate limit (`src/discord/rate_guard.py` guards *outbound* invalid-request counts).
6. `_is_authorized` is fail-open on an empty allowlist; a chat route must fail closed.
7. Ignore decisions are invisible (no log, no counter), which is why "mentions don't work" is
   hard to diagnose today.

## 4. Implementation options

### Option A — Mention → `messages` row to `supervisor-global` + a small Discord reply relay

*Sketch.* Extend the pure classifier with `classify_mention` (same file or a sibling
`src/escalations/conversation_intake.py`): requires the bot user in `message.mentions`
(not `@everyone`/role), configured channel (top level) or a thread the bot opened for a
conversation, author in a **non-empty** allowlist, not a bot, text ≤ N chars. The adapter
runs it only after escalation intake returned `False`. It inserts a message with a
deterministic ID `msg-discord-<snowflake>` (ON CONFLICT DO NOTHING), `from_kind="user"`,
`from_id="human:discord:<id>"`, `to=session:supervisor-global`, `body_kind="discord_mention"`,
`thread_id="discord:<thread id>"`, then opens a thread on the operator's message via
`ensure_thread` and posts a one-line ack. A `DiscordConversationRelay` ticked from the
orchestrator cycle polls reply rows (`to_kind="user"`, `to_id LIKE 'human:discord:%'`) that
have not been relayed and posts them into the bound thread.
*Touches.* `bot.py`, new intake + relay modules, `messages` (needs a relay cursor: a new
column or a small `discord_conversation_deliveries` table), config
(`discord.conversation.*`), prime/supervisor instructions.
*Pros.* Reuses wake/nudge/park, the supervisor's existing chat habits and dashboard history.
*Cons.* Adapter writes rows directly (the simplification spec wants "named CommandHandler
commands" for all incoming mutations); relay durability must be rebuilt; transcript-tail
replies may relay unreviewed text. *Size:* M.

### Option B — "Conversation incident" on the escalation machinery

*Sketch.* New `source_kind="operator_conversation"`; the operator's message becomes the root,
the thread is bound via `escalation_deliveries`, replies go through `accept_escalation_reply`,
supervisor answers become outbound `escalation_messages` relayed by `plan_deliveries`
(RELAY). Close = resolve.
*Touches.* `escalations` schema (nullable `project_id` or a synthetic project — the latter
contradicts the projectless design, which deleted the synthetic Global project), content
constraints, a new supervisor relay command (`escalation_note`, also useful for real
escalations), dashboard inbox filtering.
*Pros.* Inherits dedup, leases, receipts, ambiguous-send reconciliation, restart safety and
dashboard visibility.
*Cons.* Semantics inverted (escalations are supervisor-initiated asks for a human decision;
state names `needs_human`/`reply_received` mislead); the escalation inbox and the digest's
open-escalation count fill with chat; migration on a hot table. *Size:* L.

### Option C — Transport-neutral `supervisor_inbox_post` command + conversation outbox

*Sketch.* A new command family owned by the core, mirroring `escalation_reply`:
`supervisor_inbox_post` (SERVICE `<transport>:<actor>` or LOCAL principals only; refuses
body identity via the same `_FORBIDDEN_AUTHORITY_ARGS`) persists an `inbound_conversation_messages`
row unique on `(transport, external_message_id)` and enqueues the supervisor `messages` row
atomically; `supervisor_inbox_reply` (owning supervisor only) writes the outbound row and a
`conversation_deliveries` outbox entry; a Discord dispatcher delivers it with the escalation
transport. Emits `operator.message_received` so playbooks can react (the
[morning report](2026-09-24-morning-report-playbook.md) and
[voice transcripts](2026-09-24-discord-voice-transcripts.md) can consume the same seam;
`aq-inbox` email could later feed it too).
*Pros.* Identity/authz in the command layer; clean provenance (`human:discord:<id>`);
generalises to voice, email, future transports. *Cons.* New tables + migration + OpenAPI
regeneration; more surface. *Size:* L.

### Option D — Acknowledge-and-point (stopgap)

Reply to an allowlisted mention with a fixed line ("I only take replies in escalation
threads; open the dashboard: <link>") and log the reason. No supervisor involvement.
*Pros.* Tiny, fixes the "silent drop". *Cons.* Does not meet the ask; depends on the
Tailscale link fix to be useful. *Size:* S.

## 5. Initial take

Provisional: **Option C in its smallest form ("C-lite")**, built from Option A's pieces:
one command (`supervisor_inbox_post`) so the adapter never writes rows itself, the
supervisor `messages` row as the delivery mechanism (wake/nudge unchanged), and one small
outbox table keyed by a dedup key borrowed from `escalations/plan.py` conventions for
replies posted into a thread the bot opens on the operator's message. Ship Option D's
logging of ignore reasons regardless — it is free and makes the current behaviour legible.
Option B is attractive for its delivery guarantees but bends escalation semantics and needs
a nullable `project_id`; revisit only if C's outbox turns into a copy of `dispatch.py`.

Guardrails (proposed defaults):
- `discord.conversation.enabled: false` by default; refuse to enable with an empty
  `authorized_users` (fail closed; do not reuse `bot._is_authorized`).
- Only an explicit user mention of the bot in the configured channel starts a conversation;
  follow-ups in the bot-opened thread continue it (see Q3). DMs, edits, other channels,
  role/`@everyone` pings: ignored and logged at debug with the reason.
- Escalation intake keeps running first; a message it consumes is never also a conversation.
- Deterministic identity (`discord` + snowflake) makes gateway replays and reconnects no-ops.
- Per-author and global inbound limits (e.g. 10 messages / 10 min per author, text ≤ 4,000
  chars); an over-limit message gets one throttled "slow down" reaction, not a reply each time.
- Outbound: `AllowedMentions.none()`, the escalation `sanitise` rules, ≤ 2,000 chars per
  Discord message (split or link to the dashboard), shared `rate_guard`, and conversation
  replies yield to owed escalation deliveries like the digest does.
- Authority: a Discord conversation message is **not** human evidence for
  `escalation_apply_reply` and cannot resolve gates by itself; what else the supervisor may
  do on its say-so is Q1.

## 6. Open questions

1. **What may the supervisor do on a Discord instruction?** Same as dashboard chat (the
   global supervisor is elevated-global and can run every command), or a reduced policy
   (read/summarise, create tasks, pause; no delete/archive/config/restart without dashboard
   confirmation)? Decides whether the route is "remote chat" or "remote control" and how much
   of the simplification's safety argument survives.
2. **Global supervisor only, or project routing?** Route to `supervisor-global` always, or
   let `@Agent Q [project] …` / a per-project channel reach `supervisor-<pid>`? Decides
   whether the command needs a project argument and project-scope checks.
3. **Conversation shape.** Thread per mention (bot opens a thread on the operator's message)
   vs. inline replies in the channel; do follow-ups inside that thread need a re-mention?
   Decides the binding table and whether the channel stays quiet.
4. **Transcript-tail replies.** Should the delivery engine's transcript-tail fallback be
   allowed to relay to Discord, or only explicit `supervisor_inbox_reply` calls? Tail text is
   unreviewed and may contain paths/secrets.
5. **Message content intent.** Keep the privileged intent (needed for escalation thread
   replies that do not mention the bot) or drop it? Per Discord's documented policy, content
   of messages that mention the bot is delivered without the intent — **verify** before
   relying on it.
6. **Relationship to the 2026-09-08 product contract.** Is this an amendment to that spec
   (update the spec, the checklist row, `docs/guides/discord-migration.md`, and the
   negative tests) or an opt-in extension that leaves the default contract intact?
7. **Missed messages.** On a gateway reconnect without RESUME, messages are not replayed.
   Backfill with `channel.history(after=last_seen)` or accept loss? Decides whether the
   binding table stores a high-water mark.
8. **Supervisor unavailable.** Reuse `SupervisorDeliveryWatchdog`
   (`src/escalations/supervisor.py`) to post "the supervisor has not picked this up in 15 min",
   or stay silent?
9. **Dashboard visibility.** Should Discord-originated turns appear in the dashboard's
   supervisor chat history (same `messages` rows, `from_id=human:discord:<id>`) and can the
   operator continue a Discord conversation from the dashboard? Unverified how
   `chat.ts` filters by sender.
10. **Service principal scope.** Does `ExecutionPrincipal.service(...)` pass
   `_system_message_scope_error` for a projectless message, or does the new command need an
   explicit carve-out? Needs a code read of `_current_scope` population.

## 7. Dependencies and sequencing

- Independent of other roadmap items to *start*; ship the ignore-reason logging first.
- The [Tailscale link fix](2026-09-24-tailscale-dashboard-link.md) makes any "see the
  dashboard" reply useful; do it first or together.
- [Voice transcripts](2026-09-24-discord-voice-transcripts.md) reuses this intake, identity
  and outbox — it should not start before the command/outbox shape here is settled.
- [Supervisor narrative updates](2026-09-24-supervisor-narrative-updates.md) and the
  [morning report](2026-09-24-morning-report-playbook.md) need an outbound supervisor → Discord
  path too; agree on one outbox rather than three.
- Schema change → Alembic revision; command change → regenerate `openapi.json` and both
  clients (`CLAUDE.md` OpenAPI rules).

## 8. Non-goals

- Restoring slash commands, buttons, execution threads or streaming.
- A chatbot for anyone other than `discord.authorized_users`.
- The adapter mutating tasks, resolving gates or typing into sessions directly.
- DMs, multiple channels, Telegram or other transports (the command should not preclude them).
