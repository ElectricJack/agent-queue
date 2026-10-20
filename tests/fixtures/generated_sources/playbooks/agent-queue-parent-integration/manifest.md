---
playbook_id: agent-queue-parent-integration
artifact_sha256: sha256:6bac753c97dddfb8683ebab844c1b9af8ed273d00e334d7d29a75e3e5c10042c
source_sha256: sha256:685a1b0e6bdad948b759c11083692408bca7342c170782483e3e971dccc82dc1
contract_fingerprint: sha256:840b9ffe5c9a42814ae0ee6f8aa17f1b586bead5e8efd80723c7c34976ce748f
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - gate_create
  - integration_checkpoint_parent
  - integration_complete_parent
  - integration_delivery_readiness
  - integration_file_children
  - integration_parent_verify
  - integration_record_repair
  - integration_repair_dispatch
  - integration_repair_start
  - integration_repair_timeout
  - integration_transfer_owner
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Review decision

Project-scoped candidate for agent-queue train configuration. Adapted from the
historical integration graph with a new identity and current command contracts.
The parent route handles `already_completed` as idempotent success and `failed`
as failure. Commands receive durable subject identities; CI trust, Git refs,
repair budgets, and promotion authority stay server-owned. Import and activation
require operator review. The bundle has no automatic activation.

CI repair `clear-quest-21` refreshed the record and timeout command fingerprints
for the new `supervisor_recovery` action. Reviewed rules, steps, transitions,
capability grants and source text are unchanged; no-progress incidents remain
owned by the command service.

Phase 2 adds an inactive parent decision table over the existing primitives.
It selects receipt-driven collection and intent read-back, the existing failed
verification recovery, shared verifier filing/lease and exact CI verification.
Failed children, conflicts, old repair dossiers and expired writer budgets use
explicit no-default gates. Import remains write-if-absent and no activation or
engine transfer is authorized by this bundle. Operator rollout review binds the
new digest before cutover.

Integration CI repair refreshes the reconciliation contract and explicitly maps
`continued` and `superseded` to completion, and `waiting` and `target_moved` to
failure, matching the server's current outcome classifications. This preserves
the exact remote reconciliation requirement and grants no additional capability.
The new digest still requires operator review before activation.

A durably refused collection reopen now reaches the red human gate instead of
backing off forever. The `blocked-reopen` case matches the recorded
`reopen_refused` fact together with a failed verifier and routes to the
existing no-default `red` gate; it adds no command, transition or capability,
and the marker it reads is scoped to one episode, operation, head and
generation, so a later generation clears it.

## Publisher retirement repair (2026-10-12)

Removed event rules and recorded grants for retired publisher commands. Candidate
construction and publication belong to the Git train. Retained recovery, release
and cleanup rules compile against the live registry; the authored integration
policy tables are unchanged. The refreshed digest requires operator review
before activation.
