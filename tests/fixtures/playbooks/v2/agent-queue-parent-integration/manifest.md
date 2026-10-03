---
playbook_id: agent-queue-parent-integration
artifact_sha256: sha256:358641ae6ad325b7e9df53f824c868054ff68a55ea2301d422b4e3616de226cf
source_sha256: sha256:a41c408ec411fb8202e30003655529d277f950efafd1c7dc00c3f4e2e318fc32
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
