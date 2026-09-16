---
playbook_id: ci-main-sentinel
artifact_sha256: sha256:1cc1543ddd60b11d61cdaa2c33cc82be38c5d0af4003a9bdd7bcc887f41afc00
source_sha256: sha256:cf5f1254705d7f48a87ebc84ff9722373b2f3e1e635c4f4285fce53789652a51
contract_fingerprint: sha256:fc02b43b5a5539531ca8363cb8a0f2eed8d69f0f3e070becd5a3547da0b2332c
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - ci_baseline_status
  - ci_repair_adopt
  - ensure_task
  - escalation_create
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Playbooks V2 artifact manifest — `ci-main-sentinel`

This manifest binds the immutable artifact to its source digest, command-contract
fingerprint, referenced profiles, and declared capabilities. Import and activation
perform structural, scope, contract, profile, and event validation mechanically.
Policy approvals, when desired, belong in a custom playbook rather than AQ core.
