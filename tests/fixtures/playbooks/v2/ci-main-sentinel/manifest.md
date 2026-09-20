---
playbook_id: ci-main-sentinel
artifact_sha256: sha256:b2a6181d5993d7288fc3acca53292f97a4d866548e55b76ffd29e0aa7778b21d
source_sha256: sha256:a70b1e9ec935b1dac657be961b81900269afcf95736a1baa1a57ee743e05a2f9
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
