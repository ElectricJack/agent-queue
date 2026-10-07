---
playbook_id: default-pipeline
artifact_sha256: sha256:d3fe27a59ae4c3644ffb04aad79f6318eed241980325646d7832f2ee7ff166ce
source_sha256: sha256:a34ae4f0a406f1f171861246943dc023eb9dae1a955981eba2320d996afd86fa
contract_fingerprint: sha256:82a81a1588d65c999b24e78bbf360fd2aa86f5f0612c65a74806262c4fa07fcd
questions_resolved: 0
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

Recorded 2026-09-11 for policy-simplification task agile-glacier.2 from the
supervisor-corrected live source. The commit rule dispatches only for a `human`
gate resolved `approve` or `approved` that names a proposal, and hands the gate
and project to `task_batch_commit`, which re-checks the exact decision. Every
`not_approved`, `rejected` or `runtime_error` outcome reaches a `failed`
terminal. Spec ingest is a role task; its `standard-high` input is a class hint,
and the role profile supplies its execution class. Supersedes the
integration-only artifact `sha256:7881d3089a34…`.
