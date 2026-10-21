---
playbook_id: default-pipeline
artifact_sha256: sha256:2aa5acd9e4d2e7399b57edd2625bc850b45427d3c87d1568af109475bdc09b29
source_sha256: sha256:b761ab57cc68323dbe0d62b9ef55e05ad16b655a1aa09bfe737e9bda4733ce02
contract_fingerprint: sha256:a0f47bce57968a66c1be8eefe5e1f3c6174773fd8d384c34ef4a5f6d52126fcd
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

Updated 2026-10-20 for `sound-flare-53`: explicitly retain the decision's
ingestion task receipt after completion, closure as obsolete, and archive.
Rebuilt against the current command contracts, including `gate_create`;
the three rules and their transitions are unchanged. Approval paths remain
display mirrors, while immutable decision receipts establish deduplication.
