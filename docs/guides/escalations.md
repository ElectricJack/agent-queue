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

The event bus publishes these versioned state hints after commits:

- `escalation.created.v1`
- `escalation.reply_received.v1`
- `escalation.updated.v1`
- `escalation.delivery_status.v1`

Every payload requires `version: 1`, `escalation_id`, and `project_id` plus its event-specific
identity/status fields. Consumers must reload authoritative escalation state; event replay is
never permission to repeat an external send or recovery.
