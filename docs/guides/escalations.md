# Durable human escalations

Escalations are project-owned conversations between an authenticated human and the logical
project supervisor. They are core state: Discord is only one transport, and disabling it does
not disable persistence, dashboard access, or supervisor delivery.

The CLI and typed HTTP API expose six versioned-contract commands:

| CLI | HTTP operation | Contract |
|---|---|---|
| `aq escalation create` | `escalation_create` | Owning supervisor or trusted core source; source/incident replay returns the same row. |
| `aq escalation list` | `escalation_list` | Scoped summaries with pending delivery status. |
| `aq escalation get` | `escalation_get` | Authoritative incident, immutable message history, deliveries, and action receipts. |
| `aq escalation reply` | `escalation_reply` | Dashboard human or trusted external adapter only; reply and supervisor notice commit atomically. |
| `aq escalation update` | `escalation_update` | Owning supervisor CAS update using `expected_revision`. |
| `aq escalation apply-reply` | `escalation_apply_reply` | Owning supervisor applies one verified reply through its bound question, human-gate, or task-recovery service. |

Every command returns `success: true` and its typed value on success. Refusals return
`success: false`, a stable `error_code`, and an operator-facing `error`. Relevant codes are
`invalid_request`, `not_found`, `out_of_scope`, `spoofed_identity`, `identity_conflict`,
`stale_revision`, `invalid_state`, `invalid_binding`, `human_evidence_required`, and
`action_failed`.

Caller identity, human status, verified actor, project supervisor owner, transport authority,
and thread binding are not request fields. They come from the server request principal. A
trusted external adapter invokes the reply command with a service principal named
`<transport>:<verified-external-actor>` after it validates its own allowlist and channel/thread
mapping. The command derives the stored actor and transport from that principal. A supervisor
session cannot call `escalation_reply`, so supervisor-authored text cannot become human
evidence.

`escalation_apply_reply` binds `escalation_id`, `reply_id`, `expected_revision`, `action_kind`,
`target_id`, and `idempotency_key`. `decision=retry|hold` is additionally required for
`task_recover`. The core verifies that the immutable inbound reply belongs to the escalation,
that it was accepted through the human boundary, and that the target is the original
human-required question, human gate, or recovery incident. It reserves an action receipt
before invoking the existing guarded service. Reusing the same idempotency key returns the
receipt and never repeats the action.

## Supervisor triage and worker questions

A terminal blocked-task notice is a request to investigate, not itself a human escalation.
The project supervisor reloads the task explanation, exact attempt and log tail, comments,
claim, gates, recovery history, and any integration operation before deciding. Dependency
waits, active retry legs, and unchanged queue state do not create human incidents. If an
integration operation owns repair or verification, its controls and existing retry/time budgets
remain authoritative.

Completed-turn worker questions also route first to the logical
`supervisor-<project_id>` mailbox. The persisted question retains its session instance token,
task, agent, and claim epoch. Narrow factual questions may be answered by the owning supervisor;
human-required or ambiguous questions use `aq question escalate`, which creates/reuses a durable
escalation with the question as its source. Question reads expose that source link as
`escalation_id`. Direct human `question_answer` calls are refused: a human responds with
`escalation_reply`, and the supervisor applies that evidence with `escalation_apply_reply`.

Supervisor mailboxes survive absence and restart. Messages target logical project ownership,
never a historical session row. After the configured supervisor-delivery timeout (15 minutes by
default), the watchdog creates at most one `supervisor_delivery` operational incident per source
notice. That incident only reports unavailability; its source kind cannot be applied as approval
for a question, gate, or recovery. A failed evidence-bound action records an outbound follow-up
in the same escalation conversation and returns the incident to `reply_received` rather than
resolving it.

The event bus publishes these versioned state hints after commits:

- `escalation.created.v1`
- `escalation.reply_received.v1`
- `escalation.updated.v1`
- `escalation.delivery_status.v1`

Every payload requires `version: 1`, `escalation_id`, and `project_id` plus its event-specific
identity/status fields. Consumers must reload authoritative escalation state; event replay is
never permission to repeat an external send or recovery.

## Discord delivery: one post and one thread per incident

External delivery is a thin renderer over that durable state (implementation spec §7). It is
driven from the `escalation_deliveries` outbox by `src.escalations.EscalationDeliveryService`,
which the daemon ticks once per orchestrator cycle when a Discord bot is present and
`discord.escalation.enabled` is true. With no bot — or with the setting off — the incident, the
supervisor loop and the dashboard inbox are unchanged; only the channel goes quiet.

What the channel sees:

| Delivery kind | When | Where |
|---|---|---|
| `root` | The incident exists and is not closed | One channel post plus the one thread it opens |
| `ack` | A human reply was persisted | In the thread, once per reply |
| `relay` | A correlated supervisor message | In the thread |
| `resolution` | The incident reached a terminal state | In the thread, then the root is edited and the thread archived |

The root post carries the project, task title and ID, the blocker, the exact decision needed,
the configured mention, a dashboard link and the escalation ID. It is the only message that may
mention anybody, and only the IDs in `discord.escalation.mention_user_ids` /
`mention_role_ids`: every interpolated string — task titles, summaries, supervisor text, human
replies — is neutralised first, so authored text can never produce a ping. The resolution edit
drops the mention entirely.

Delivery identity is durable, not in-memory:

- one row per `(escalation, kind, generation)` via the outbox's unique `dedup_key`, so replayed
  events, a gateway reconnect and a second daemon all converge on the same post;
- `claim_escalation_deliveries` leases a row, so only one sender owns it at a time and an
  expired lease is reclaimed rather than duplicated;
- a restart rebinds from the stored channel/message/thread IDs — never from a task-thread
  heuristic.

An external send cannot be transactionally exactly-once with the database, and the code does
not pretend otherwise. Each message embeds an `aq-esc:<dedup key>` marker; after an ambiguous
timeout the next attempt searches recent history for it. If the marker is found the delivery is
recorded `sent` with that receipt and nothing is re-posted. If it is not found, the row becomes
`unknown` — attention-needed, visible on `escalation_get` and counted by `digest_status` —
rather than being blindly re-posted.

A deleted root or thread earns an explicit replacement generation: at most one may be pending
at a time, and a closed incident never gets one, so a resolution can never reopen work. Missing
channel or missing permission is an actionable delivery fault recorded on the row; the daemon
never creates a channel. Retries are bounded (six attempts, 15s → 30m backoff) and the existing
Discord invalid-request rate guard holds escalation sends rather than dropping them.
