---
playbook_id: agent-queue-parent-integration
artifact_sha256: sha256:a514441d2d7dd73a4de0795fbfc4e5e14d1fe07dfb9152ec2a3c602c82333d77
source_sha256: sha256:a9a971a014a9409f5a21b602ed23df8126551b480a44f6d104a70bce2e677ec4
contract_fingerprint: sha256:a4b6e09f12c8c3111b1987f4d09fd86c45b973a1cc83b9d689f566d4e937b4c0
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - delivery_promote
  - gate_create
  - integration_checkpoint_parent
  - integration_complete_parent
  - integration_delivery_readiness
  - integration_file_children
  - integration_parent_verify
  - integration_reconcile_promotion
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
