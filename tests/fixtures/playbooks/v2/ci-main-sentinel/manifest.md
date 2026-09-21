---
playbook_id: ci-main-sentinel
artifact_sha256: sha256:33536f8bbd14f8562e77623fbc9103a90807a963b5f8fe0a1e4598361ad034b0
source_sha256: sha256:345f379e6f4f4ba5e58cf70dff8b1e574a4987f7fee013d3a7f047a5e0567a4c
contract_fingerprint: sha256:c9fb78fb30185ab6b6868dae25a9f36e6bd225fd1a213bfe05cfba05b8196806
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - ci_baseline_status
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
