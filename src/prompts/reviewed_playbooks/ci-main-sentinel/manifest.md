---
playbook_id: ci-main-sentinel
artifact_sha256: sha256:e646e28a561597810f81a589515a5fe48cbbbf6779bd1a5fe338dfdf5ba2bfd1
source_sha256: sha256:ec1658aceabaf2e5dc42957f5a53b5a7e73b06dd9ec648496b3b39fd73ce8323
contract_fingerprint: sha256:ba2727e567a5cece321516ac8dd42d139895446d767816a8d78587001ca5aa7b
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
