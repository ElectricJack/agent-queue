---
playbook_id: agent-queue-root-train
artifact_sha256: sha256:8e34c15d9c3dfe6ba22f600113e6e4bbdd118a453504d61608c4aae42f91ea65
source_sha256: sha256:da4f0cd28522b2e85654c5019f96ed0c7b560db2746550eaae561a0edb295d07
contract_fingerprint: sha256:1b70a6bcef122a31aa89b507432f8cd94b7e750b698ad6538e6669e4384e6bbd
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
