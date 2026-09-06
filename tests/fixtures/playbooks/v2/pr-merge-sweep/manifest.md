---
playbook_id: pr-merge-sweep
artifact_sha256: sha256:593a5fd6368773797603f0e9541edb7b9bcbf417b0636b42179b8846d4254d43
source_sha256: sha256:fa8a07f2fad26a7386b7ce415feb51d29f550b983ea467955c1fc718206690eb
contract_fingerprint: sha256:20421bfbc33f9409092a3979fad734fe4dff3b9976df757b64154f7c96935ea4
questions_resolved: 2
capabilities_granted:
  aq_commands:
  - ensure_task
  - task_route
  harness_tools: []
  plugin_tools: []
profiles_referenced:
- pr-merger
---

# Playbooks V2 artifact manifest — `pr-merge-sweep`

This manifest binds the immutable artifact to its source digest, command-contract
fingerprint, referenced profiles, and declared capabilities. Import and activation
perform structural, scope, contract, profile, and event validation mechanically.
Policy approvals, when desired, belong in a custom playbook rather than AQ core.
