---
playbook_id: agent-queue-parent-integration
artifact_sha256: sha256:d561465f0ec8ef33931d65720bb413736c47f646512a5d9fc54a0cb4b70a161a
source_sha256: sha256:1de9265e7bba73272a2777fb17e042bd81014e5cc3c7e4214a6a5471eab5d200
contract_fingerprint: sha256:8c7f0f87bdffdf6827d49588c1fc0ef3d2823fd68ae6d6af8f81090d328c27d3
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
