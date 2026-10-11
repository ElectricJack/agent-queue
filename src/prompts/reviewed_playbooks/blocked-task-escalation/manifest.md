---
playbook_id: blocked-task-escalation
artifact_sha256: sha256:a7a6c40ca45d0b1c41dd0647689098d7634a507b328597e62b6e918bc6f90afb
source_sha256: sha256:c8c28fe1de804c51348bfca9c976bddc0d298905c58b9cfc3cb24d104e3e673e
contract_fingerprint: sha256:04aa97ad67d622f1fb05d821c989e1ae5ba1223247371f779299b227ee51d9e3
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - task_recovery_notify
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Playbooks V2 artifact manifest — `blocked-task-escalation`

This manifest binds the immutable artifact to its source digest, command-contract
fingerprint, referenced profiles, and declared capabilities. Import and activation
perform structural, scope, contract, profile, and event validation mechanically.
Policy approvals, when desired, belong in a custom playbook rather than AQ core.
