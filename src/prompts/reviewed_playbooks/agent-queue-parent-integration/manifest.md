---
playbook_id: agent-queue-parent-integration
artifact_sha256: sha256:587cdd5181963d152e8425b50f278896f0bc036cb78b84f37e3007f5c26e3622
source_sha256: sha256:1de9265e7bba73272a2777fb17e042bd81014e5cc3c7e4214a6a5471eab5d200
contract_fingerprint: sha256:e84411d236ed2ab7326f03d7a2f1e54707cbe99b6cfe514df9f152c2aaa34e07
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
