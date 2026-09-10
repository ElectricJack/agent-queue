---
playbook_id: default-pipeline
artifact_sha256: sha256:1fb7133d44c4c5b62f1ad20b4223d9ce0ce72e9b85af7042af9c4abcf755be71
source_sha256: sha256:e7d5edb20ba9394f66d8aed55c4ae1d0bf589c4b13846589b95e2690dd4b795f
contract_fingerprint: sha256:74b924d7399ee435749da1bda4c00e59e92449bdf94aadae0885884e56fdc29d
questions_resolved: 3
capabilities_granted:
  aq_commands:
  - ensure_task
  - gate_create
  - task_batch_commit
  harness_tools: []
  plugin_tools: []
profiles_referenced:
- spec-ingest
---

# Playbooks V2 artifact manifest — `default-pipeline`

This manifest binds the immutable artifact to its source digest, command-contract
fingerprint, referenced profiles, and declared capabilities. Import and activation
perform structural, scope, contract, profile, and event validation mechanically.
Policy approvals, when desired, belong in a custom playbook rather than AQ core.
