---
playbook_id: default-pipeline
artifact_sha256: sha256:719a3f794bc215a534e7fbf0a0e2800f772fdfc10b613c8c1e7c520b9562aab0
source_sha256: sha256:b11d9a8e1e7df8b7449a13cada05094bbe841450fd42990ffa6aa961f6808913
contract_fingerprint: sha256:3098cb7e8701d5ff63d7727d9ae32d1f817dac5fe499236c2c451104e1ff437d
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - ensure_fidelity_review
  - ensure_spec_ingest
  - gate_create
  - task_batch_commit
  - task_batch_release
  harness_tools: []
  plugin_tools: []
profiles_referenced:
- fidelity-reviewer
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

Updated 2026-10-22 for `nimble-lantern-37.2` (spec ingestion held batches,
L5.2): approved implementation specs are materialized held, so the pipeline
gains two rules. `fidelity-review-on-held` answers `batch.held` with
`ensure_fidelity_review` (role profile `fidelity-reviewer`), and
`release-on-receipt` answers `batch.receipt_recorded` with `task_batch_release`
for exactly the attested revision and digest. A release refused `needs_scope`
ends `completed` without release, the batch waiting on its scope ask; every
other release refusal reaches the `failed` terminal. The spec-ingest,
proposal-gate and commit rules and their transitions are unchanged apart from
the spec-ingest step text; ordinary proposals keep the human gate.
