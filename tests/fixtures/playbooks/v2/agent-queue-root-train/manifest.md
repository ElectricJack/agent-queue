---
playbook_id: agent-queue-root-train
artifact_sha256: sha256:ba3c982468daebe36942083b23c5662d8f744815723286d2e5adc32b998b88dd
source_sha256: sha256:90479b1a40be0a5a340fe185065c9a4ac5f02c797f4bf7f4edf2cb9b116801f6
contract_fingerprint: sha256:d3091151928c5dfc17d8c4e6a84503ac551f88ff33e5d9b33284c5efcce6c645
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - integration_build_candidate
  - integration_ci_evidence
  - integration_repair_close_current
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
