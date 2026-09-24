---
playbook_id: agent-queue-root-train
artifact_sha256: sha256:b4d97387fe6477bf6aef72342fb600619f71d46ca7c82bef0f5f84f1a64cce3b
source_sha256: sha256:71a471df2047632c4e18a85f32e244af7df24e5a23e15cef6677d9923f57ef62
contract_fingerprint: sha256:7b6380af1d3d88ba850d53910ba311774cfe2b802e8eb3fafe2a624bd4b74b9e
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - integration_build_candidate
  - integration_ci_evidence
  - integration_cleanup
  - integration_promote_main
  - integration_release
  - integration_repair_dispatch
  - integration_seal
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
