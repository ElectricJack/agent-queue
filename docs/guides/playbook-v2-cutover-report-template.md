# Playbook V2 cutover report

Generate the current evidence with `aq playbook cutover-report --json`; paste
the result below without editing hashes or counters. A non-empty
`blocking_reasons` list means cutover is not approved.

**Run it from a checkout.** `reviewed_by`/`reviewed_at` come from the human
decision records in `tests/fixtures/playbooks/v2/<playbook_id>/review.md`, and
the shadow-parity record from `parity-report.json` beside them — both are
checked-in files read relative to the working directory. A report generated
somewhere else finds neither and blocks for missing review evidence, which is
the intended direction: an activation whose live artifact hash no approved
review names is reported as unreviewed, never as approved.

## Evidence

- Generated at:
- Active contract fingerprint:
- Enabled artifacts (playbook, scope, artifact SHA-256, source SHA-256, health,
  reviewed by, reviewed at, V1 rollback artifact present):
- Unresolved migration entries:
- Acknowledged disabled playbooks:
- Pending events (total, oldest age, by playbook):
- Active V1 runs (running, paused, oldest age):
- Shadow parity (observations, identical, expected, unexplained, report path):
- Rollback ready:
- Cutover eligible:
- Blocking reasons:

## Signature

Approved for cutover by: ____________________

Date: ____________________

Commit SHA: ____________________
