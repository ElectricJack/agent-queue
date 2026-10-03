---
title: Discord as a chat extension of the supervisor
status: draft
date: 2026-10-03
task: crisp-zenith-64
kind: spec
reviewer: Jack (dashboard /reviews)
---

# Discord as a chat extension of the supervisor

## Intent (Jack, 2026-10-03, condensed)

Discord notifications and chat are one of the weakest parts of AQ: messages are massive
and hard to read. Discord should be an extension of the supervisor: periodic updates on
how things are going, the supervisor escalating and asking questions directly, chatting
with the supervisor in the channel without `@agent-queue`, and the supervisor posting
links to the dashboard pages Jack needs to look at. Keep the one thing that works well:
when a review is ready, a short post with a link that opens on a phone. Worst today:
escalations pile up, never clear, are hard to read, and their link goes to a settings page.

## Goals

1. Every inbound message from Jack in `#agent-queue-win` (or a thread on a bot post)
   reaches the live supervisor session; its replies post back. No mention required.
2. Every outbound post is one or two lines plus exactly one deep link. Details live on
   the dashboard, never in the post.
3. Escalations are stateful: one post each, edited in place, collapsed when resolved or
   obsolete, answerable from Discord, linked to the exact escalation page.
4. A supervisor-written digest on a cadence with quiet hours and "nothing to report"
   suppression.
5. One stable deep-link scheme for task, review, escalation, batch, session and report
   pages, reachable from Jack's phone.
6. The existing pile of 81 escalations is cleaned up by a one-shot, idempotent sweep.

## Non-goals

- Multi-user Discord. The allow-list is one person. Nothing here designs for a team.
- Rich embeds, buttons-as-navigation, or slash-command menus. Slash commands stay as
  they are (`src/discord/slash_commands.py`) and are not part of the chat surface.
- Replacing the dashboard. Discord is the notification and chat layer; the dashboard is
  where detail and action live.
- Supervisor judgment in code. Which facts to mention and how to phrase them is a
  playbook. The daemon owns cadence, budgets, delivery, state and links.

---

## 1. Inventory today

### 1.1 Components (`src/discord/`)

| Module | Status | Role today |
|---|---|---|
| `bot.py` | live | discord.py client, gateway, interaction dispatch |
| `inbound.py` | live | routes gateway messages: escalation thread → escalation intake; everything else → conversation classifier, which drops it unless `discord.conversation.enabled` (`inbound.py:94-96`) |
| `escalation_intake.py` | live | replies inside an escalation thread become `escalation_messages` rows (the only inbound path that works today) |
| `escalation_transport.py` | live | `post_root`, `ensure_thread`, `post_thread_message`, `edit_root`, `archive_thread`, `find_marker`, `read_history` (lines 163-265). Edit and archive exist but nothing drives them to a resolved state |
| `cutover.py` | live | resolves the single legacy channel name to `channel_id` at startup (261-283); back-fills legacy questions and pending gates as escalations (140-177) |
| `rate_guard.py` | live | per-channel send guard; counts sends, not edits |
| `slash_commands.py` | live | `/aq …` commands; untouched by this spec |
| `notifications.py` (1113 lines) | dead code | only imported for formatting helpers; no legacy notification handler is wired (`src/main.py:357`) |
| `embeds.py`, `views.py` | dead code | embed/button builders for the retired notification path |

Adjacent producers: `src/escalations/render.py` and `transport.py` (escalation posts),
`src/reviews/notifier.py` (review-ready posts), `src/digest/` (deterministic digest),
`src/reports/` (morning and hourly reports), `src/conversations/` (the @mention
conversation route: `intake.py`, `preconditions.py`, `outbox.py`, `envelope.py`,
`render.py`, `limits.py`, `backfill.py`, `maintenance.py`).

### 1.2 Config on this box (`~/.agent-queue/config.yaml`)

- Still carries the retired `discord.channels` block (`control`, `notifications`,
  `agent_questions` all `agent-queue-win`) and `per_project_channels`.
- No `discord.channel_id`, no `discord.authorized_users`, no
  `discord.conversation.enabled`. Cutover resolves the channel name to an id at start.
- `dashboard.server.public_url = http://100.73.221.21:5173` (Tailscale IP, Vite dev
  port). Port 5173 is listening. Nothing listens on 8082 (`DEFAULT_DASHBOARD_SERVER_PORT`),
  so the brief's ":8082" is the default, not what this box runs.
- Morning report on, 07:00 PT, to `discord:1477498117430579240`, cap 1500 chars.
- Hourly supervisor report off (`reports.hourly.enabled` unset, bundle
  `supervisor-hourly-report` not activated, fleet not full).

### 1.3 Message catalogue (everything the bot sends)

| # | Post | Producer | Trigger | Size today | Link target today | Verdict |
|---|---|---|---|---|---|---|
| 1 | Escalation root | `escalations/render.py:109-137` via `transport.post_root` | `escalation.created.v1` | ≤1900 chars, 400/field, 5-7 lines: summary, investigation, decision, choices, Details link | `{base}/settings/messaging#escalation-reply-{id}` (render.py:85-93) | wrong page, too long, never edited to resolved |
| 2 | Escalation thread name | `render.py` MAX_THREAD_NAME_CHARS 90 | first reply | 90 | n/a | fine |
| 3 | Escalation thread message (ack / supervisor reply) | `render.py:146-162` via `post_thread_message` | reply or ack | ≤1900 | settings page | too long, wrong page |
| 4 | Escalation "no reply needed" notice | `render.py:162` | operational source kinds | ≤1900 | settings page | should not be in the human channel at all |
| 5 | Review ready / revised | `reviews/notifier.py:106-127` via `post_root` | new review revision | unbounded (`changes_note` not capped) | `{base}/reviews/{id}` (desktop route) | **broken**: 38 recent `50035 content > 2000` failures. The post Jack loves is currently not arriving |
| 6 | Digest (deterministic) | `digest/render.py` | `digest.*` schedule | ≤1200, 3 highlights × 120 | footer: bare dashboard root | 0 rows ever sent on this box |
| 7 | Morning report | `reports/` via `outbound_deliveries` | 07:00 PT | ≤1500 | `{base}/reports/{id}` (desktop) | 7 sent; the only periodic post that works |
| 8 | Hourly supervisor report | `reports/hourly.py` | hourly when enabled | ≤1500 | `dashboard_href(task_path)` | off |
| 9 | Conversation reply | `conversations/render.py`, MAX_REPLY_CHARS 1900 | supervisor reply to an @mention | ≤1900 | none | never fires (see 1.4) |
| 10 | Conversation ack / refusal | `conversations/` | @mention accepted / refused | 1 line | none | every @mention refused today |
| 11 | Slash command responses | `slash_commands.py` | `/aq …` | varies | varies | out of scope |

Inbound today: 4000-char cap (`conversations/limits.py` MAX_INPUT_CHARS). Only
escalation-thread replies reach anything.

### 1.4 Why plain messages and @mentions never reach the supervisor

Five independent gates, each sufficient to drop the message:

1. **Feature flag off.** `discord.conversation.enabled` is unset on this box, so
   `inbound.py:94-96` drops every non-escalation-thread message as
   `ignored:preconditions_unmet`.
2. **Mention required.** Even when enabled, `classify_conversation`
   (`conversations/intake.py:173-175`) ignores any top-level message that does not
   mention the bot. Only a thread already bound to a conversation is mention-free.
3. **Preconditions never met in production.** `conversation_preconditions`
   (`preconditions.py:46-74`) needs `authorized_users`, `guild_id`, `channel_id`, a
   complete cutover and a *bound outbox*. The orchestrator always constructs
   `UnboundOutbox()` (`src/orchestrator/core.py:401-403`) and `src/main.py` never binds
   a real one, so `outbox_unbound` is permanently unmet. The route cannot be turned on
   by config at all. This is the root defect.
4. **Wrong recipient.** The route addresses `SUPERVISOR_RECIPIENT` (supervisor-global,
   `conversation_queries.py`); `supervisor_inbox_reply` accepts only the live global
   launch and never `supervisor-<project>`. Jack's supervisor is
   `session:supervisor-agent-queue`.
5. **No return path.** Nothing the supervisor sends reaches Discord: `user:*` recipients
   are marked platform-delivered with no subscriber (`src/messages/delivery.py:279-285`),
   and the supervisor profile grants no Discord-posting command.

DMs are ignored unconditionally (`intake.py:159-160`).

### 1.5 Why escalations pile up (anatomy of the 81, operator DB, read-only)

| Bucket | Count | Cause | Should be |
|---|---|---|---|
| `gate` | 35 (11 with an already-resolved gate) | cutover back-fill of legacy review gates (`cutover.py:160-177`); nothing closes an escalation when its gate resolves | auto-resolved |
| `supervisor_delivery` | 33 (13 open, 20 stale) | `escalations/supervisor.py:58-100` opens one per undelivered `task_recovery` notice at severity **high** and posts it to the human channel | operational notice to the supervisor, coalesced, never a human post |
| `needs_human` with COMPLETED task | 10 | task finished; escalation left open | auto-obsolete |
| `needs_human`, no task | 40 | project-level questions and retired sources | supervisor triage, most obsolete |
| resolved, ever | 1 | | |

States: 56 needs_human, 24 stale, 1 resolved. Nothing transitions a post after creation
except a human reply in its thread, so the channel is an append-only log of every
incident the daemon ever had.

---

## 2. Conversation: the channel is the supervisor's inbox

### 2.1 Identity and allow-list

- `discord.authorized_users: ["<Jack's Discord user id>"]` is the whole allow-list.
  Snowflake ids only, never names. Required for the route to enable (already enforced by
  `conversation_preconditions`).
- A message from anyone else, any bot (including the aq bot itself) or any webhook is
  dropped with a logged `ignored:not_authorized`; no reply, no reaction. Silence is the
  only correct response to a stranger.
- Button interactions carry the interacting user's id and pass the same check.
- DMs: off by default (`intake.py:159`). `discord.conversation.allow_dm: true` admits a
  DM from an allow-listed user as a channel message with `thread_id = dm:<channel>`.
  Replies go back to the DM. Default stays off until Jack asks.

### 2.2 Routing rule (replaces "mention required")

For a message that passes the allow-list, in the bound channel or a thread whose parent
is the bound channel:

| Where | Becomes |
|---|---|
| Top level in the channel | a turn in the **channel conversation** (one durable conversation row per channel, `conversations.kind = channel`) |
| Thread on an escalation root | escalation reply (unchanged, `escalation_intake`) |
| Thread on a review-ready post | review conversation turn, tagged `review_id` |
| Thread on a digest or morning report | conversation turn tagged `digest_id` |
| Thread on any other bot post, or a thread Jack starts | conversation turn, thread bound to a new conversation row (existing `find_conversation_by_thread` path) |
| Mentions the bot | same as above; the mention is stripped by `normalise_text` and changes nothing |

The two-gate structure in `classify_conversation` stays: escalation threads never become
chat; the change is that a top-level message no longer needs `mentions_bot`.

### 2.3 Delivery to the supervisor

- Recipient resolution, in order: the live `supervisor-<project>` session for
  `discord.project_id` (new key; defaults to the only project when there is exactly one,
  else required), then the live global supervisor, else *queued*. This replaces the
  hard-coded global recipient.
- Delivery is the existing session inbox: the turn is an `agent_message` to
  `session:<supervisor>` with `body_kind = discord_conversation`, injected by the
  composer like any other inbox message. The envelope (`conversations/envelope.py`)
  carries `conversation_id`, `thread_id`, author, text, and the tag (`review_id`,
  `escalation_id`, `digest_id`) so the supervisor knows what the thread is about.
- The supervisor answers with `aq message reply <id> --body "..."`. The conversation
  outbox (bound for real, see 7.1) posts the reply to the originating thread or channel,
  split at `MAX_REPLY_CHARS` (1900) into at most two posts; anything longer is cut with
  "…" and a link to `/focus/conversations/<id>`.
- `supervisor_inbox_reply` accepts replies from whichever launch holds the live
  supervisor session named on the envelope, project or global.

### 2.4 When no supervisor session is live

- The turn is stored (`conversation_turns.state = queued`). The bot posts **one** status
  line per conversation, edited in place rather than appended:
  `⏸ Supervisor is offline. 1 message queued; I'll answer when it starts.` with the count
  updated as more arrive.
- Supervisor start drains the queue in order into the inbox and the status line is
  edited to `▶ Supervisor is back; answering now.` then deleted once the first reply
  posts.
- After `SUPERVISOR_DELAY_SECONDS` (900) with the queue non-empty, **one**
  `supervisor_delivery` escalation is opened for the conversation (incident key
  `supervisor-unavailable:conversation:<conversation_id>`), severity medium, routed to
  the dashboard inbox only, not the channel (see 5.5). It auto-resolves on drain.

### 2.5 What the supervisor may say and do from chat

The supervisor profile gains, under `## Capabilities`, grants for `message reply`,
`escalation resolve`, `escalation answer`, and `digest post` (the commands in 4 and 5).
Nothing else changes in the profile: deciding what to do with Jack's message is the
supervisor's job, and it uses the same `aq` commands it has today.

---

## 3. Message design

### 3.1 Voice

- First person, present tense, as the supervisor: "Merged X. Y is stuck on CI; I've
  reopened it. Nothing needs you."
- Plain words, no headings, no bullet lists, no code blocks, no embeds, no tables.
- One leading status glyph per post from a fixed set: `✅ landed`, `⏳ working`,
  `⚠️ stuck`, `❓ needs you`, `📄 review`, `📊 digest`, `⏸ offline`. Nothing else.
- Exactly **one** link per post, always the last line, bare URL wrapped in `<…>` so
  Discord does not render a preview card.
- Ids appear once, as the link, never repeated in prose.

### 3.2 Size budget (enforced by the renderer, not the author)

| Post | Lines | Chars | Overflow rule |
|---|---|---|---|
| Review ready | 2 + link | 300 | title cut at 120 with "…"; `changes_note` capped at 160 (fixes 1.3 #5) |
| Escalation root (open) | 2 + link | 400 | decision line kept, summary cut |
| Escalation root (resolved / obsolete) | 1 | 160 | |
| Escalation thread reply | ≤4 + link | 600 | rest behind the link |
| Digest | 3 + link | 600 | each section cut to 2 lines |
| Chat reply | free | 1900, max 2 posts | "…" + conversation link |
| Offline / status line | 1 | 120 | |

The renderer refuses to send over budget: it truncates and appends the link. A producer
cannot opt out. `tests/test_discord_render_budgets.py` pins every row of this table.

### 3.3 Examples

```
📄 Spec for review: Discord as a chat extension of the supervisor (rev 2)
Rev 2 answers the threading and quiet-hours questions.
<https://aq.example.ts.net/focus/reviews/rev-123>
```

```
❓ nimble-torrent-66 needs a decision: keep the Opus 5.5 trial or revert to Sonnet?
Reply here, or tap a choice on the page.
<https://aq.example.ts.net/focus/escalations/escalation-abc>
```

```
✅ Resolved: kept the Opus trial (your reply, 14:02).
```

```
📊 14:00. Landed: 3 tasks, including the knowledge-records restore.
Stuck: calm-current on CI twice; I've reopened it with the trace. Needs you: 1 review.
<https://aq.example.ts.net/focus/inbox>
```

---

## 4. Periodic digest written by the supervisor

### 4.1 Cadence and quiet hours

- `digest.cadence_minutes` default 120; `digest.quiet_hours` default `22:00-07:00`
  `America/Los_Angeles` (the daemon's report timezone). The 07:00 morning report is the
  first digest of the day and stays on its existing schedule; the deterministic digest
  schedule is retired.
- Windows are aligned to the hour; one `outbound_deliveries` row per window
  (`dedup_key = digest:<project>:<window_start>`) so a restart never double-posts.

### 4.2 Content and who writes it

Mechanism (daemon): `aq digest facts --since <window_start>` returns a bounded JSON of
landed tasks, stuck tasks (failed twice, blocked > N min, parked deliveries), open
escalations and reviews needing Jack, session counts, and the previous digest's facts
hash. Policy (playbook `supervisor-digest`): the supervisor turns the facts into three
sentences, landed / stuck and what I'm doing / needs you, and calls
`aq digest post --window <start> --body "..."`. The daemon renders within the 600-char
budget, appends `/focus/inbox` and posts.

Fallback: if no `digest post` arrives within 10 minutes of the window, the daemon posts
the deterministic digest from the same facts (today's `digest/render.py`, re-budgeted to
600). The cadence never silently stops because the supervisor was busy or down.

### 4.3 "Nothing to report" suppression

- The window is skipped when the facts hash equals the previous window's hash and there
  is nothing in "needs you".
- After 3 consecutive skipped windows, one line is allowed per day:
  `⏳ Quiet: 4 sessions working, nothing needs you.` so silence is distinguishable from a
  dead bot.
- Quiet hours suppress posting, not fact collection; the 07:00 report covers the night.

---

## 5. Escalations as stateful items

### 5.1 One post, edited in place

Each escalation has exactly one root post (`external_root_message_id`, already stored).
Every state change edits that post through `edit_root`; nothing appends a second root.
The thread is created lazily on the first reply (`ensure_thread`, unchanged).

### 5.2 State machine

```
open ──reply/button──▶ answered ──supervisor ack──▶ resolved
  │                                                   ▲
  ├──source gone (task terminal, gate resolved,       │
  │  notice delivered)────────────────────────▶ obsolete
  └──stale timer (existing)───────────────────▶ stale ──▶ obsolete after 7d
```

| State | Root post | Thread |
|---|---|---|
| open | `❓ <decision> \n <one-line context> \n <link>` | as created |
| answered | `💬 Answered by Jack 14:02; supervisor is acting on it. \n <link>` | open |
| resolved | `✅ Resolved: <≤120 chars outcome>` | archived |
| obsolete | `⚪ No longer needed: <reason>` | archived |
| stale | `⏳ Still open since <date>; <link>` | open |

Resolved and obsolete posts are never deleted; the one-line form is the "collapsed"
state Discord can express. `discord.escalations.delete_collapsed_after_hours` (default
off) exists for Jack to opt into deletion.

### 5.3 Answerable from Discord

- When the escalation carries `choices`, the root post has one button per choice (≤5)
  plus `Reply…`. A press from an allow-listed user records an inbound
  `escalation_messages` row with `verified_actor` and the choice, exactly as a thread
  reply does, and the post moves to `answered`. Presses from anyone else get an ephemeral
  "not authorized" and nothing is recorded.
- Thread replies keep working unchanged (`escalation_intake`).
- The supervisor resolves with `aq escalation resolve <id> --outcome "..."`; the daemon
  edits the root and archives the thread.

### 5.4 Links

`/focus/escalations/:id`: a phone-sized page with the decision, the one-line context,
the task link, choice buttons, a reply box and Resolve. Never `/settings/messaging`.
`escalation_url()` in `render.py:85` becomes a thin call to the shared link resolver (6.2).

### 5.5 Auto-resolution and routing rules (mechanism)

| Source kind | Rule |
|---|---|
| `gate` | resolves when the gate resolves (`gate.resolved.v1`); outcome = the gate decision |
| `supervisor_delivery` | severity low; posts to the dashboard inbox and the supervisor's inbox, **never** the human channel; auto-resolves when the notice delivers or the supervisor session starts; coalesced to one incident per (project, body_kind) |
| `question` / `needs_human` with a task | obsolete when the task reaches a terminal status |
| any `stale` | obsolete after 7 days with no activity |

### 5.6 Back-fill: the existing pile

`aq escalation sweep [--apply]` (also `aq doctor --check escalations.pile --fix`),
idempotent, run once at rollout:

1. Resolve the 11 gate escalations whose gate is already resolved.
2. Mark the 33 `supervisor_delivery` escalations obsolete (notice delivered or stale).
3. Mark the 10 `needs_human` with a COMPLETED task obsolete.
4. List the 40 task-less `needs_human` rows to the supervisor's inbox for triage; those
   whose source project is inactive or whose question text is from a retired source are
   marked obsolete; the rest stay open and are summarised in the next digest as
   "N old questions still open".
5. Edit every affected root post to its collapsed form and archive its thread, at the
   rate guard's pace (≈1 edit/s), so the channel ends with ≤10 open items and 70 one-line
   closed ones.

Dry run prints the plan per escalation. The sweep records
`escalation_messages(direction=system, text="sweep: <rule>")` so the audit trail shows
why each item closed.

---

## 6. Deep links

### 6.1 URL scheme

| Object | Phone (focus) URL | Desktop URL | Exists |
|---|---|---|---|
| task | `/focus/tasks/:id` | `/tasks/:id` | yes |
| session | `/focus/sessions/:id` | `/sessions/:id` | yes |
| report | `/focus/reports/:id` | `/reports/:id` | yes |
| review | `/focus/reviews/:id` | `/reviews/:id` | focus: **new** |
| escalation | `/focus/escalations/:id` | `/focus/escalations/:id` (no desktop page today) | **new** |
| batch | `/focus/batches/:id` | `/projects/:p/graph?batch=:id` | **new** |
| PR | GitHub PR URL, direct | | from the batch / task record |
| needs-you inbox | `/focus/inbox` | `/reviews` + escalations | **new** |
| conversation | `/focus/conversations/:id` | `/conversations` | **new** |

Focus routes are phone-first; on a wide viewport the shell redirects to the desktop
route where one exists. Ids are aq ids, so URLs are stable across restarts and
re-deploys.

### 6.2 One resolver

`DashboardLinks.resolve()` (already used by the review notifier) is the only source of
the base URL, and `dashboard_href(path)` the only way to build a link. Every producer in
1.3 goes through it; `tests/test_dashboard_links.py` asserts no renderer emits
`/settings/` and that every post carries exactly one link.

### 6.3 Remote access from a phone

Today the base URL is the box's Tailscale IP on the Vite dev port
(`http://100.73.221.21:5173`). That works on the tailnet but is HTTP, dev-server-bound and
changes if the port does. Recommendation, in order:

1. Serve the built dashboard from the dashboard server (`:8082` default) and set
   `dashboard.server.public_url` to it.
2. Put Tailscale Serve in front (`tailscale serve https / http://127.0.0.1:8082`), and
   set `public_url` to `https://<box>.<tailnet>.ts.net`. Jack's phone on the tailnet then
   gets HTTPS, MagicDNS and a URL that survives port changes. No public exposure.
3. `aq doctor --check dashboard.public_url` verifies: set, HTTPS or tailnet address,
   host:port listening, and that a GET of `/focus` returns the shell.

Needed from Jack: Tailscale on the phone (already implied by the current IP), and a
decision on 1 vs 2 (default: 2).

---

## 7. Migration, rollback, rate limits, tests

### 7.1 Phases (each behind a flag; rollback = flag off)

| Phase | Change | Flag | Rollback |
|---|---|---|---|
| P0 fix what is broken | cap review posts (3.2); bind a real conversation outbox in `src/main.py`; escalation links to `/focus/escalations`; `supervisor_delivery` leaves the human channel | none needed (bug fixes) | revert |
| P1 stateful escalations | state machine, edit-in-place, buttons, auto-resolution, `/focus/escalations`, `/focus/inbox`, sweep | `discord.escalations.stateful` | off: today's create-only posts |
| P2 conversation without @ | routing rule 2.2, project supervisor recipient, queue + offline line, `/focus/conversations` | `discord.conversation.enabled` + `discord.conversation.require_mention: false` | `require_mention: true` restores today's behaviour |
| P3 supervisor digest | facts command, playbook, `digest post`, fallback, quiet hours | `digest.supervisor_authored` | off: deterministic digest at the same cadence |

Config migration (doctor fix): drop the retired `discord.channels` and
`per_project_channels` blocks; write `discord.channel_id`, `discord.guild_id`,
`discord.authorized_users`, `discord.project_id`. Schema: new columns
(`conversation_turns.state`, `escalations.collapsed_at`, `escalations.outcome`) are
additive, inspector-guarded revisions.

### 7.2 Rate limits

Discord allows roughly 5 messages per 5 s per channel and counts edits; 429s carry a
retry-after. Rules:

- One global outbound token bucket, 20 operations/min, shared by posts and edits
  (`rate_guard.py` extended to count edits).
- Escalation edits coalesce: at most one edit per escalation per 30 s, latest state
  wins; state edits are queued, never dropped.
- Digest ≤1 per window; chat replies ≤1 per inbound turn (split posts count as 2).
- The sweep (5.6) runs at ≤1 operation/s and resumes from where it stopped.

### 7.3 Tests

- `tests/test_discord_render_budgets.py`: every row of 3.2, including the review
  `changes_note` cap (regression for the 50035 failures).
- `tests/test_conversation_intake.py`: allow-list (stranger, bot, webhook, DM default,
  DM opt-in), top-level without mention, each thread binding in 2.2, escalation thread
  never becomes chat.
- `tests/test_conversation_wiring.py`: production wiring binds a non-`UnboundOutbox`
  outbox and `conversation_preconditions` is met with the documented config (regression
  for 1.4 #3); recipient resolution project → global → queued.
- `tests/test_escalation_state.py`: state machine, edit-in-place idempotency, button
  press by allow-listed vs stranger, each auto-resolution rule in 5.5, coalescing.
- `tests/test_escalation_sweep.py`: fixture built from the five pile buckets in 1.5; the
  sweep is idempotent and the dry run matches the apply.
- `tests/test_dashboard_links.py`: scheme table 6.1, no `/settings/` ever, one link per
  post.
- `tests/test_digest_supervisor.py`: window dedup, quiet hours, suppression and the
  once-a-day quiet line, fallback after 10 minutes.
- `tmux`-marked: supervisor inbox round trip (inbound turn → inject → reply → outbox).
- Dashboard: focus route tests for `reviews`, `escalations`, `inbox`, `conversations`.
- `scripts/e2e-smoke.sh` unchanged; no claims, pools or hierarchy are touched.

---

## 8. Decisions requested from Jack

1. Remote access: Tailscale Serve HTTPS (recommended) or keep the tailnet IP and port?
2. DMs: leave off (default) or enable?
3. Collapsed escalations: keep as one-line posts forever (default) or delete after N hours?
4. Digest cadence 2 h and quiet hours 22:00-07:00 PT acceptable?
5. The 40 task-less old questions: supervisor triage as in 5.6, or mark all obsolete?

---

## 9. Implementation breakdown (child tasks to route after approval)

Design only in this task. After approval, file these as routed children of an epic, in
phase order; P0 may ship as a single batch.

| # | Task | Area | Acceptance |
|---|---|---|---|
| 1 | Cap review-ready posts and route through the link resolver | `src/reviews/notifier.py` | budget test; no 50035 in a 24 h log |
| 2 | Bind the conversation outbox in production; project supervisor recipient | `src/main.py`, `src/orchestrator/core.py`, `conversation_queries.py` | wiring test; `@mention` reaches `supervisor-agent-queue` |
| 3 | `supervisor_delivery` escalations: low severity, coalesced, never the human channel, auto-resolve | `src/escalations/supervisor.py`, transport | state test; no new high-severity delivery posts |
| 4 | Escalation state machine, edit-in-place, collapsed forms, auto-resolution | `src/escalations/`, `escalation_transport.py` | state tests |
| 5 | Choice buttons + `aq escalation resolve` + supervisor grants | `src/discord/`, `src/commands/`, supervisor profile | button test; profile drift check |
| 6 | Dashboard focus routes: `escalations`, `reviews`, `inbox`, `batches`, `conversations` | `dashboard/src/pages/focus/` | route tests; phone screenshot in the task |
| 7 | Link resolver as the single source; renderer budgets | `src/escalations/render.py`, `src/digest/render.py`, new `src/discord/render_budget.py` | links test, budgets test |
| 8 | Escalation sweep + doctor check; run against the operator DB with Jack present | `src/commands/escalation_commands.py`, `src/doctor/` | dry run reviewed, apply leaves ≤10 open |
| 9 | Conversation routing without mention; queue + offline line; DM opt-in | `src/conversations/intake.py`, `inbound.py`, outbox | intake tests; tmux round trip |
| 10 | Supervisor digest: facts command, `digest post`, playbook bundle, fallback, quiet hours | `src/digest/`, `src/prompts/reviewed_playbooks/supervisor-digest/` | digest tests; one real window observed |
| 11 | Config doctor fix: retire `discord.channels`, write the new keys; `dashboard.public_url` check | `src/config.py`, `src/doctor/` | doctor tests; this box migrated |
| 12 | Delete dead `notifications.py`, `embeds.py`, `views.py` after 1-7 land | `src/discord/` | import graph clean, ruff clean |
