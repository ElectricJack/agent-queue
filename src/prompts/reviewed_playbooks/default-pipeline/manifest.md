---
playbook_id: default-pipeline
artifact_sha256: sha256:04498620587d606388c43e77b5cb492ac98f131aeea594c83041b79d4ec7c1c2
source_sha256: sha256:58b1df380eb21e7162101c4e98173ad0703ab29b12ef3eb13f9657b647042303
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

Updated for `wise-forge-11`: explicitly declare the three typed command
capabilities and retain the complete executable graph. No collection, scan,
notification episode or script replaces any existing operation. Decision and
graph receipts remain owned by their commands; the spec-ingest role retains
feedback and supersession accounting, including Jack's sign-off for drops.

The supervisor identified installed-source drift: the installed 2026-10-06
source (`a34ae4f0…`) still uses path-based `ensure_task` ingestion. The authorized
baseline for this port is the shipped decision-receipt source
`sha256:b761ab57cc68323dbe0d62b9ef55e05ad16b655a1aa09bfe737e9bda4733ce02`,
artifact `sha256:0064f7e4b84733ae2d190fa6a420d48a8de100b13b5c406a0e770db5f893cafb`.
It is captured unchanged in
`tests/fixtures/playbooks/scenarios/default-pipeline/before.artifact.json`.
`tests/test_default_pipeline_equivalence.py` compares its dispatch, command
arguments, receipts and outcomes with this bundle. Existing intake tests cover
real handler effects, decision replay after closure/archive and supersession
instructions. This recording does not install or activate the new policy.
The installed drift is deliberate: the supervisor's H08-OPS cutover remains
under a human hold. Publication of this bundle does not release that hold.
