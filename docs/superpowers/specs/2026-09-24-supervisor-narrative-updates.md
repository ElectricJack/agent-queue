# Supervisor narrative updates (hourly) — preliminary spec

**Date:** 2026-09-24 · **Status:** preliminary — analysis and options, no decision taken
**Roadmap:** [operator feedback roadmap](2026-09-24-operator-feedback-roadmap.md)
**Related:** [morning report playbook](2026-09-24-morning-report-playbook.md) (shares the brief → supervisor → submit → outbox primitive proposed here),
[agent sleep/wake](2026-09-24-agent-sleep-wake.md) (the shared "queue without waking / wake on policy" primitive),
[wake context compaction](2026-09-24-wake-context-compaction.md) (hourly wakes grow the supervisor's resumed context),
[Discord mention routing](2026-09-24-discord-mention-routing.md) (the inbound half of "supervisor as hub"),
[Discord voice transcripts](2026-09-24-discord-voice-transcripts.md),
[Tailscale dashboard link](2026-09-24-tailscale-dashboard-link.md) (the link the narrative ends with);
existing: [Discord simplification §8](2026-09-08-discord-simplification-implementation.md), `docs/specs/design/supervisor-agent.md`,
[projectless supervisor](2026-08-30-projectless-supervisor-design.md), `docs/guides/escalations.md`.

## 1. The ask

Operator brief ("Supervisor as the communication hub"):

- Every hour, the supervisor receives completion events and writes a **narrative** — what got
  done, where things stand, what is left — instead of a bare task list.
- It uses a **strong model through the agent** (the supervisor session), not a cheap model
  called inline from a playbook (the brief names Gemini Flash).
- It is **playbook-defined**, so anyone running Agent Q can configure or disable it.
- Framing: "one completion-event hook plus the inbound routing fix unlocks hourly updates, the
  morning report, and voice drops." This spec owns the outbound hourly half and the hook.

## 2. What exists today

**An hourly digest already ships, and it was deliberately built LLM-free.**
`src/digest/` is a pure, clock-free layer (package docstring `src/digest/__init__.py`: "no Discord
objects, no network, no LLM call, and no wall clock of its own"):

- `facts.py` — `WorkFact` (stable `key`, `kind` ∈ provider/reroute/completed/progress/started,
  `category` ∈ `CATEGORIES = ("work","vcs","budget","system")`), `ActiveTask`, `DigestInputs`,
  `DigestWindow` (half-open UTC `[since, until)`, `catchup` flag).
- `eligibility.py` — the §8 send/silence table plus dedup against already-reported keys and
  repeated highlight wording (`highlight_key`, `evaluate_eligibility`).
- `render.py` — one message, `MAX_CHARS = 1200` (line 30); `sanitise()` (line 49) drops mention
  tokens, neutralises `@`, strips backticks and tracebacks.
- `aggregate.py` — `build_digest()` (line 45), the single entry point shared by `digest_preview`
  and real delivery.
- `schedule.py` — `destination_id()` (`discord:<channel_id>`), `config_generation()` (hash of
  channel, interval, project_ids, categories, catch-up), grid-aligned window bounds.
- `dispatch.py` — `DigestScheduleService`. `evaluate()` (line 221) **reserves** the
  `digest_windows` row first (unique on `destination, config_generation, window_start,
  window_end`; `src/database/tables.py:1560`), then writes either a payload or a
  `suppression_reason`. `pump()` (line 322) is a separate lease-driven sender that stands down
  while any escalation delivery is owed (`escalation_priority=orch.db.count_due_escalation_deliveries`,
  wired in `src/main.py:347`) or the rate guard is hot. `_deliver_one()` (line 420) appends an
  `aq-dig:<hash>` marker so an ambiguous send can be reconciled via `transport.find_marker`,
  records `unknown` rather than reposting, and drops windows older than `catchup_hours`.
- Claim predicate: `claim_digest_windows` selects `send_status IN ('pending','retry') AND due_at <= now`
  (`src/database/queries/escalation_queries.py:~1481`). `evaluate()` currently sets
  `due_at=window.until` (`dispatch.py:239`) — **`due_at` is therefore already a free "hold until"
  lever** that needs no schema change.
- Evidence read: `collect_digest_activity` (`src/database/queries/digest_queries.py:166`) —
  completions (`task_completion_records`), attempt starts, `task_comments.kind='progress'`,
  `pr_url` milestones, provider transitions and reroute batches; "active" = a live non-stale
  attempt (`live_attempt_predicate`). It explicitly refuses `list_recent_task_activity`
  (`activity_queries.py:63`) because that one counts `updated_at` churn.
- Ticked from the orchestrator cycle (`src/orchestrator/core.py:2884`); commands `digest_preview`
  / `digest_status` (`src/commands/digest_commands.py:99,165`); Settings → Messaging panel.
- The design record is explicit: "No LLM call is required to build a digest"
  (discord-simplification §8, line 153).

**The supervisor is a session, not code.** `src/supervisor.py` was deleted (see the banner in
`docs/specs/supervisor.md`). The supervisor is the `supervisor` profile
(`src/profiles/defaults/supervisor/profile.md`: `harness: claude`, `default_class: deep-high`,
`lifecycle: named`, `mode: on_demand`, `wake_mode: resume`, `idle_timeout: 2700`,
`needs_workspace: false`). Addresses: `supervisor-global` (projectless, `SUPERVISOR_AGENT_ID`
in `src/agents/configuration.py:12`) and `supervisor-<pid>` → runtime `n-supervisor--<pid>`
(`src/messages/session_lens.py:75-89`). The installation-wide digest's natural author is
`supervisor-global`.

**How work reaches the supervisor today.** Only through durable `messages` rows:
`MessageDeliveryEngine.run_delivery_pass` (`src/messages/delivery.py:74`) nudges an idle session,
waits for a busy one, and **wakes a sleeping on_demand session** (line ~123). Established
event → supervisor pattern: a system playbook calls a contracted, idempotent notify command
that writes one message —
`supervisor-failure-triage.md` → `task_failure_triage_notify` (`src/commands/task_commands.py:4452`),
`provider-failover` → `provider_availability_notify`, which writes to `("session","supervisor-global")`
and `("user","dashboard")` (`src/providers/availability_service.py:1369`). Messages carry a
`body_kind` (existing values: `task_recovery`, `agent_question`, `escalation_reply`,
`integration_stuck_batch`, `development_validation_infrastructure`) and `archive_after_inject`.
`SupervisorDeliveryWatchdog` (`src/escalations/supervisor.py`) detects a supervisor that has not
taken delivery within `discord.escalation.supervisor_delivery_timeout_minutes` (default 15).

**There is no completion → supervisor hook.** `task.completed` (schema `src/event_schemas.py:143`,
required `task_id, project_id, title`) is consumed by integration, vibecop and the default
pipeline, not by the supervisor. There is also no digest event on the bus.

**The supervisor cannot post to Discord.** Discord is notification-only; nothing in
`src/discord/` subscribes to `message.*` (verified by grep), and discord-simplification §5
table says "arbitrary Discord messages are not supported". The only outbound paths are the
three daemon-owned outboxes sharing `DiscordEscalationTransport` (`post_root`,
`ensure_thread`, `post_thread_message`, `edit_root`, `find_marker`;
`src/escalations/transport.py:71`): escalations, the digest, and `ReviewNotifier`
(`src/reviews/notifier.py`).

**Playbook ways to reach a strong model.**
- `AgentTaskStep` (`src/playbooks/definition.py:266`) creates a task — but the supervisor profile
  is refused as a task executor (`src/profiles/task_execution.py`, `task_execution_profile_error`).
- `LlmStep` (`definition.py:225`) with `transport: "cli"` runs a one-shot, **tool-free** call
  through a logged-in agent CLI (`_execute_cli`, `src/playbooks/executors/llm.py:299`;
  `src/llm/cli.py`) — a strong subscription-billed model without an API key, schema-validated.
  `transport: "api"` uses the direct path (`src/llm/`, default provider `anthropic`).
- `CommandStep` → `message_send` / a notify command → the supervisor session.
- `wait` steps can wait on an `event` (`WAIT_KINDS`, `src/playbooks/waits.py:26`), e.g.
  `message.replied` (schema carries `message_id`, `reply_id`, `body`; `event_schemas.py:702`).

**Triggers.** `timer.{N}m|h` and `cron.HH:MM` (host-local time) from `src/timer_service.py`;
timer events are global (`project_id: null`).

## 3. Gaps

1. No hook tells the supervisor about completions, and no primitive lets an event be recorded
   for an agent *without* waking it (every message to a sleeping on_demand session wakes it).
2. No path for supervisor-authored text to reach Discord; adding one naively would bypass every
   §7/§8 guarantee (reserve-before-send, marker reconciliation, escalation priority, rate guard,
   mention neutralisation, one message per window).
3. Nothing is playbook-defined in the digest: interval, categories and project selection are
   `discord.digest.*` config, and the scheduler is daemon code.
4. No quiet-hours concept anywhere (grep for `quiet_hours` finds nothing).
5. No accounting of what an hourly supervisor wake costs; the supervisor's resumed session keeps
   growing with every wake.

## 4. Implementation options

### Option A — Replace the digest with a supervisor-authored post

Sketch: a system playbook on `timer.1h` messages `supervisor-global` "write the hourly update";
the supervisor gathers state with `aq` and calls a new `discord_post` command.

- Touches: new outbound command + outbox, new playbook, retire `DigestScheduleService`.
- Pros: simplest mental model; the narrative is the only surface.
- Cons: throws away reserve-before-send idempotency, silent-window persistence, catch-up
  coalescing and dedup, or forces us to rebuild them around an LLM. A supervisor that is asleep,
  rate-limited or on an unavailable provider means **no update at all**. `timer.1h` is not
  grid-aligned and not per-window durable. Gives every supervisor turn a Discord write, which
  the simplification spec deliberately removed.
- Size: M to build, but a regression in guarantees. **Not recommended.**

### Option B — Feed digest facts to the supervisor as input; supervisor posts separately

Sketch: keep the digest unchanged; after each evaluated window, send the supervisor the window's
facts; the supervisor's narrative goes out through its own new outbox as a second message.

- Touches: new message `body_kind`, new outbox table + pump, new submit command.
- Pros: digest untouched; narrative can be longer and richer.
- Cons: **two posts per hour** (contradicts §8's "one short channel message"); a second outbox
  duplicates all of `dispatch.py`'s delivery code; the two can disagree.
- Size: M–L.

### Option C — Narrative rides on the digest's window and delivery machinery (hybrid)

Sketch:

1. `DigestScheduleService.evaluate()` is unchanged up to the deterministic payload. When
   narrative mode is on and the window is eligible, it reserves with
   `due_at = window.until + narrative_grace` (e.g. 10 min) instead of `window.until`, and
   records `payload.narrative = {"state": "requested"}` next to the deterministic `text`.
2. The daemon emits a new bus event, e.g. `digest.window_ready` `{window_id, destination,
   window_start, window_end, eligible, completed_count, active_count, catchup}`.
3. A shipped **system playbook** (`supervisor-hourly-narrative`) triggers on it and calls one
   contracted, idempotent command, e.g. `digest_narrative_request(window_id)`. The command
   writes **one** message to `supervisor-global` (`body_kind="digest_narrative"`, natural
   idempotency on `window_id`) whose body is the brief: the window's `WorkFact`s grouped by
   project, active tasks, open-escalation count, the previous narrative's text (for "where
   things stand" continuity), the character budget and the submit instruction.
4. The supervisor may investigate (`aq task explain`, comments, `task_recent_activity`) and
   calls `digest_narrative_submit(window_id, text)`. The command CAS-writes
   `payload.narrative = {"state":"submitted","text":…,"by":session}` only while the row is still
   `pending` and unclaimed, **re-sanitises** (mention tokens, `@`, length cap, no tracebacks),
   and pulls `due_at` forward to `now` so the pump sends promptly.
5. `pump()`/`_deliver_one()` sends `narrative.text` if submitted, else the deterministic
   `text` once `due_at` passes. Same marker, same row, same receipt. One message per window.
6. Disabling the playbook (or `discord.digest.narrative.enabled: false`) leaves the digest
   byte-identical to today.

- Touches: `src/digest/dispatch.py` (due_at hold, payload choice), `src/event_schemas.py`
  (new event), two commands + contracts in `src/commands/contracts/builtin.py` and
  `digest_commands.py`, the supervisor profile's `aq_commands` grant list (plus the reviewed
  bundle's frozen capability fingerprint — see `src/playbooks/required.py` docstring), a new
  reviewed bundle under `src/prompts/reviewed_playbooks/`, config for grace/budget, dashboard
  preview showing "narrative pending / submitted / fell back".
- Pros: keeps **every** §8 guarantee — one window, one row, one post, catch-up, dedup, marker
  reconciliation, escalation priority, rate guard. The fallback is structural: the
  deterministic text is already on the row; the supervisor can only upgrade it. Suppressed
  (idle) windows never wake the supervisor, so cost scales with active hours. The "which
  agent, what prompt, whether at all" decision lives in a playbook, as asked.
- Cons: the digest is no longer strictly LLM-free *in what it may send* (the builder still is);
  up to `narrative_grace` of added latency; the supervisor must write inside a ~1,150-char
  budget (or we raise the cap for narrative mode — Discord's hard limit is 2,000); a brief with
  facts from selected projects is sent to an admin-scope supervisor that can see everything
  (scope leakage is instruction-enforced, not structural).
- Size: M.

### Option D — Narrative written by a one-shot CLI `LlmStep`, not the supervisor session

Sketch: same as C, but the playbook's step 3 is an `LlmStep` with `transport: "cli"`,
`profile_id` of a deep-class profile, the brief as `inputs`, `output_schema {text}`, and a
following `CommandStep` → `digest_narrative_submit`.

- Pros: no session wake, no growing context, schema-validated, hard `AiBudget`
  (`max_calls`, tokens, `timeout_seconds`), still a strong model on the operator's
  subscription (not Gemini Flash). Cheapest and most predictable.
- Cons: tool-free (`_execute_cli` refuses tool use), so it cannot investigate beyond the brief;
  it is not "the supervisor", so the narrative does not accumulate in the hub's memory and
  cannot connect to what the operator told the supervisor earlier. Diverges from the brief's
  "through the agent".
- Size: S on top of C (C's commands and event are the substrate either way).

## 5. Initial take

Provisionally **Option C**, with **Option D as a configurable alternative author** behind the
same `digest_narrative_submit` command. Rationale:

- The digest's machinery answers the hard questions the brief does not mention (restart
  double-posts, outages, silent hours, dedup, escalation priority). Replacing it (A) or
  duplicating it (B) spends that work again.
- "Feed digest facts to the supervisor" and "ride on the digest's delivery" are not
  alternatives — C does both: facts in, one post out.
- The deterministic digest becomes the fallback for free, which is exactly what "supervisor
  down" needs.
- The completion-event hook for the hourly case is **the window itself**: the facts are already
  a durable, deduplicated completion feed. A per-`task.completed` wake is unnecessary for an
  hourly product and would wake a deep-high session dozens of times an hour. The per-event hook
  is still worth building for voice drops / interactive use, but as the sleep/wake spec's
  "record without waking" primitive (see §7), not as a wake.

This is provisional: the answer to Q1 (does the operator want the narrative to replace the
digest's content, or accept a 1,200-char narrative) could push toward B.

**Suppression / quiet hours.** Inherit the digest eligibility table (nothing happened → no
row eligible → no wake). Add *narrative* quiet hours (`discord.digest.narrative.quiet_hours`,
host-local or configured tz) during which eligible windows send the deterministic digest
without waking the supervisor — i.e., quiet hours suppress cost, not information. Whether quiet
hours should suppress the post entirely is Q4. Also skip the wake for "active-only" windows
(no new facts, only `active_count`) — the deterministic one-liner says everything.

**Cost.** Per eligible window: one resumed deep-high turn = cached prior context + brief
(~1–3k tokens of facts) + a few `aq` tool calls + ≤~400 output tokens. The dominant term is
the resumed context size, not the brief — which is why compaction matters (§7). We should
measure from the token ledger (`supervisor-global` rows are projectless) before choosing
defaults, and expose `narrative.max_per_day` as a hard cap. Unverified numbers are deliberately
not given here.

**Fallback conditions** (any → send deterministic text, record `narrative.state` =
`skipped:<reason>` on the payload): playbook inactive; `supervisor-global` absent/unlaunchable
(e.g. its provider is unavailable per `Orchestrator.provider_availability`); supervisor has an
older `digest_narrative` message still undelivered (don't stack); grace deadline passed;
submitted text fails validation; daily cap reached; catch-up window (a catch-up covering many
hours is better as facts).

## 6. Open questions

1. **Narrative length and shape.** Keep the 1,200-char §8 target, or allow up to ~1,900 for
   narrative mode, or root post + thread? Decides whether C fits the operator's intent or B is
   needed.
2. **Author: session (C) vs one-shot CLI (D) as the shipped default?** Decides cost profile and
   whether the narrative can investigate and "remember". Could be a playbook variant choice.
3. **Which supervisor?** `supervisor-global` for the fleet message, or per-project supervisors
   each drafting a paragraph that the global one merges? Per-project costs N wakes/hour.
4. **Quiet hours semantics:** suppress only the wake (deterministic still posts) or the post too
   (and then fold into the morning report)? Also: timezone source — there is no install tz
   config; `cron.*` uses host local time (`TimerService._now_local`).
5. **Grace period and latency budget.** How long may a window wait for the narrative (5/10/15
   min)? Longer grace = more fallbacks avoided, later posts.
6. **Scope leakage.** `supervisor-global` has admin scope; `discord.digest.project_ids` narrows
   the destination. Is instruction + a post-submit check (e.g. reject text mentioning task ids
   outside the window's facts) enough, or must the brief/supervisor be structurally scoped?
7. **Continuity vs dedup.** §8 forbids reposting identical highlights. A narrative saying "still
   working on X" each hour is a repeat by design. Is "where things stand" allowed to restate
   in-flight work, and how do we stop it becoming noise?
8. **Does the supervisor's context become a liability?** Twenty-four hourly briefs/day in one
   resumed conversation. Should narrative briefs be `archive_after_inject` and the supervisor
   instructed to compact, or should narrative turns run in a separate named session
   (`supervisor-narrator`)? Couples to the compaction spec.
9. **Generation semantics.** Does toggling narrative mode roll `config_generation`
   (`schedule.py:50`)? It changes what a window *sends*, not what it *means*; rolling it would
   restart windows unnecessarily.
10. **Where does the brief come from** — reuse `DigestInputs` verbatim (guaranteed consistent with
    the fallback text) or enrich with completion `summary`/`changes` from
    `task_completion_records`? Enrichment makes better prose but a bigger prompt.
11. **Dashboard:** should the narrative also land in the dashboard (Messaging panel history,
    supervisor chat), so Discord-less installs get it? The `digest_windows.payload` already
    persists it.
12. **Evaluation:** how do we judge narrative quality (hallucinated progress is the §8 sin)?
    A check that every task id/title in the text appears in the brief?

## 7. Dependencies and sequencing

1. **Shared primitive with [agent sleep/wake](2026-09-24-agent-sleep-wake.md):** "deliver a
   message to an agent without waking it; wake by policy (schedule, threshold, explicit)". C
   does not strictly need it (the window *is* the batching), but the per-completion hook for
   voice drops and interactive use does, and the narrative brief should be expressible as
   "wake now with these accumulated items" once it exists. Agree the message-level flag
   (`wake: false`/`wake_policy`) there, not here.
2. **[Wake context compaction](2026-09-24-wake-context-compaction.md)** before enabling C by
   default: hourly resumed wakes are the heaviest steady consumer of supervisor context.
3. **Build order:** (a) `digest.window_ready` event + `due_at` hold + payload choice in
   `dispatch.py`, behind config, no author yet (fallback path exercised end to end);
   (b) `digest_narrative_request` / `digest_narrative_submit` commands and contracts;
   (c) supervisor profile grants + `aq doctor --check profiles.system_drift` path for existing
   installs (`--grants-only` reseed); (d) reviewed bundle, shipped in
   `src/prompts/reviewed_playbooks/`, **not** required (`REQUIRED_SYSTEM_PLAYBOOK_IDS`), probably
   not default-on until cost is measured; (e) optional D variant.
4. The [morning report](2026-09-24-morning-report-playbook.md) should reuse (b)'s
   request/submit shape; decide whether to generalise it into a "supervisor report" outbox
   before building either (see that spec's Q on generalising `digest_windows`).
5. [Tailscale dashboard link](2026-09-24-tailscale-dashboard-link.md) improves the link the
   narrative ends with (`remote_link_base` in `src/main.py`), independent otherwise.
6. [Discord mention routing](2026-09-24-discord-mention-routing.md) is the inbound half; not a
   prerequisite for the outbound narrative.

## 8. Non-goals

- Letting the supervisor post arbitrary Discord messages. It submits text for a daemon-owned
  window; the daemon sends.
- Changing escalation delivery or its priority over the digest.
- Per-task or per-project message fan-out (§8 decision 2 stands).
- Mentions in routine updates (§1 decision 5 stands; submitted text is re-sanitised).
- Replacing `digest_preview` semantics — it stays the deterministic dry run (it may *show*
  that narrative mode is on).
- Streaming or real-time updates; the unit stays the window.
