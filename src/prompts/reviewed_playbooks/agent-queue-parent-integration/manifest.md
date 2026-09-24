---
playbook_id: agent-queue-parent-integration
artifact_sha256: sha256:a6a39410cf600b46cf90619966d604ae99284d48cf037f60b575a8d1068532eb
source_sha256: sha256:1de9265e7bba73272a2777fb17e042bd81014e5cc3c7e4214a6a5471eab5d200
contract_fingerprint: sha256:785231157149d5982aca42894c2c0fb73653624f9b1d65303afb9fbfbb73d50e
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
