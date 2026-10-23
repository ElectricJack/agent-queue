---
id: supervisor-hourly-report
name: Supervisor hourly narrative report
version: 1
scope: system
enabled: false
triggers:
  - digest.window_ready
  - timer.1m
---

# Supervisor hourly narrative report

Optional authoring policy; disabled until the operator explicitly enables it
and `reports.hourly.enabled`. It is never a required readiness playbook.
Eligible digest windows already contain a frozen brief, visibility snapshot,
deadline and deterministic fallback before either rule runs. A completion event
never wakes the supervisor. Request reservations cap author turns and do not
backlog them. A missing playbook uses immediate deterministic delivery.

The only author is the assigned supervisor-global session. No LLM step, worker
task, arbitrary Discord post or model fallback belongs to this policy. It reads
`aq report brief ID`, performs bounded evidence reads and submits with
`aq report submit ID --file FILE`. Submission includes brief_hash,
expected_version, text and evidence_refs. Explain what completed, what was
published and what delivered to main separately. Every shipment claim needs a
delivery reference; label inference and pending/unknown delivery. Omitted facts
are omissions, not proof that nothing happened. Prior narrative is context only.
Do not initiate code work for a report. The server inserts links and the marker,
neutralizes mentions, and enforces the 1,200-character final budget. Deadline or
pump claim freezes the winner; there is one logical post and no later edit.

## Rule: request-window

1. Call `report_reconcile` without inputs and bind the result as `requests`.
   The command queues the one durable author message for each still-reserved
   eligible hourly request. Duplicate events reuse the same message.

## Rule: recover-window

1. On `timer.1m`, call `report_reconcile` without inputs and bind the result
   as `requests`. Lost events and restarts recover from durable requests.
   Closed, disabled, narrowed-scope and expired requests never wake an author.

## Failure handling, uniformly

Both rules end on `completed`; `rejected` or `runtime_error` fail visibly.
No inline retry or author fallback exists. The next minute reconciles durable
state. The existing digest pump delivers deterministic text at the deadline,
behind escalations and the transport rate guard.
