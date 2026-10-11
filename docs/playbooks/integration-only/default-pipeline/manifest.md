---
playbook_id: default-pipeline
artifact_sha256: sha256:afd035806c1b0a36d6927b638359b8e210bd8c4dbc82307f94af0b1cc27945d0
source_sha256: sha256:92af62c6c1142f7888b290fc61b0483977c46cb49910f7d21f7357f4ac51ca10
contract_fingerprint: sha256:0d87be85423d42e1945155c4bf76d2c58b0fcd04052bfafaae419a36972d06c3
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - ensure_spec_ingest
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

Updated for H03, approved implementation review `rev-sharp-zenith` revision 1.
The ingestion rule consumes immutable decision identity through `ensure_spec_ingest`.
The role and dedup key are server-owned, with one task receipt per decision.
The other two rules retain their exact human-gate checks and failure terminals.
