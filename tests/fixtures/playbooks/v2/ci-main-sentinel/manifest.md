---
playbook_id: ci-main-sentinel
artifact_sha256: sha256:c823bd901fd1bfd5ddae922a04a4c89828d1399834f6f2a007e53b292a0f894f
source_sha256: sha256:45d286d59b7500db112d54d529b330ddef3cfdd369c98b386fe789dcb55aa7e9
contract_fingerprint: sha256:83a8885d3b020ef45ca509de66220769e48629923d360bf9e05ef28c7549bec9
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - ci_baseline_status
  - ensure_task
  - gate_create
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Playbooks V2 artifact manifest — `ci-main-sentinel`

This manifest binds the immutable artifact to its source digest, command-contract
fingerprint, referenced profiles, and declared capabilities. Import and activation
perform structural, scope, contract, profile, and event validation mechanically.
Policy approvals, when desired, belong in a custom playbook rather than AQ core.
