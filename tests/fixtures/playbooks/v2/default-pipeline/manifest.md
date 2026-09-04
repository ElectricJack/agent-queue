---
playbook_id: default-pipeline
artifact_sha256: sha256:36f25f93328d04b1fe2fc07b630d4481c0e2bd5bcc573ac26b57011b784f6bdf
source_sha256: sha256:889c839015d2b2f91aa22d46fd8c49a1782d6e66a4e055dbeaad43ffb053aa95
contract_fingerprint: sha256:64868157d0d987401d13d954e0bd3edc0c01fc427c626b2947d760a57cc855fe
questions_resolved: 3
capabilities_granted:
  aq_commands:
  - add_dependency
  - ensure_task
  - gate_create
  - get_downstream_tasks
  - task_batch_commit
  harness_tools: []
  plugin_tools: []
profiles_referenced:
- final-reviewer
- reviewer
- spec-ingest
---

# Playbooks V2 artifact manifest — `default-pipeline`

This manifest binds the immutable artifact to its source digest, command-contract
fingerprint, referenced profiles, and declared capabilities. Import and activation
perform structural, scope, contract, profile, and event validation mechanically.
Policy approvals, when desired, belong in a custom playbook rather than AQ core.
