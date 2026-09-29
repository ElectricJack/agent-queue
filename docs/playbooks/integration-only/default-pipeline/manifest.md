---
playbook_id: default-pipeline
artifact_sha256: sha256:89278f6a5e96f7b304d0e9eef55eb66675eb6f09b86e0a2ac4f9483d8fe95c5a
source_sha256: sha256:2f6b6e830b40507896e94a9924a468bce41fa1e0449c9955cec0dd5f74f07cb8
contract_fingerprint: sha256:53629e63bfd9954bf9a53b4095f66291f08b06bb8f93cdaeef773c5cfd5e1455
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
