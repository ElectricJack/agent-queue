---
playbook_id: default-pipeline
artifact_sha256: sha256:32a2cdb9fe7ad734151e1ec5c9f5c94c867aa6acd937705d11e91027d69d8186
source_sha256: sha256:a8602b6932f72311297d3a6b8b27c3c19383ea8022397eda7777cf1b6ac74e42
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

Updated 2026-10-13 for `calm-falcon-54`: the executable spec-ingest task
description carries the spec supersession checklist. Authors account for every
approved predecessor goal as kept, changed or dropped; reviewers flag every
drop and require Jack's explicit sign-off for the goal and replacement revision.
Missing accounting or unsigned drops are reported to the supervisor before
affected implementation work is committed. This adds agent instructions,
not a new command-level semantic approval gate; the three rules and their
success/refusal transitions are unchanged.
