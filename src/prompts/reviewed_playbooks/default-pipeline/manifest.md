---
playbook_id: default-pipeline
artifact_sha256: sha256:cc1a871e376dda943af1b19c11ca43986edead0f312a35cacc9d65d678faae06
source_sha256: sha256:c93344160dbc33eb1822efbc11350a8007388d5e4ebb02300bb64cac1e45da16
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
terminal. Spec ingest carries an explicit `standard-high` route. Supersedes the
integration-only artifact `sha256:7881d3089a34…`.
