---
playbook_id: default-pipeline
artifact_sha256: sha256:4f7e3a99fbc9355563bfe89ce17e77e5f0f37f321874eef799fd101652ef8c74
source_sha256: sha256:c93344160dbc33eb1822efbc11350a8007388d5e4ebb02300bb64cac1e45da16
contract_fingerprint: sha256:80bb436a99cc90a4acd14ef45a001e19721f1073a7e12fa6465d16eb507955fb
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
