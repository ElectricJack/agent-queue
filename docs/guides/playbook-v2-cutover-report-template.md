# Playbook V2 cutover report

Generate the current evidence with `aq playbook cutover-report --json`; paste
the result below without editing hashes or counters. A non-empty
`blocking_reasons` list means cutover is not approved.

A non-empty `evidence_errors` list means the daemon could not read one of the
sources it weighs — an unread source is never rendered as a clean one, so each
entry is also a blocking reason and the section it fed is marked
`unavailable: true`. Fix the read and regenerate; do not sign a report that did
not see the whole fleet.

**Run it from a checkout.** For shipped playbooks, `reviewed_by`/`reviewed_at`
come from the human decision records in
`tests/fixtures/playbooks/v2/<playbook_id>/review.md`, and the shadow-parity
record from `parity-report.json` beside them — both are checked-in files read
relative to the working directory. Project-scoped playbooks instead carry an
attributed review of their exact active hash in the activation database row.
A report generated somewhere else finds no shipped review fixtures and blocks
for missing review evidence. In every scope, an activation whose live artifact
hash no approved review names is reported as unreviewed, never as approved.

## Evidence

- Generated at:
- Active contract fingerprint:
- Enabled artifacts (playbook, scope, artifact SHA-256, source SHA-256, health,
  reviewed by, reviewed at, V1 rollback artifact present):
- Unresolved migration entries:
- Acknowledged disabled playbooks:
- Pending events (total, oldest age, by playbook, unavailable):
- Active V1 runs (running, paused, oldest age, unavailable):
- Evidence sources that could not be read (must be empty):
- Shadow parity (observations, identical, expected, unexplained, report path):
  the record is checked, not counted — it must name its suite, corpus, V1
  source and artifact hash, classify every observation it counts over a
  non-empty corpus, and carry an artifact hash equal to the bytes each
  deterministic playbook actually activates. A recompiled artifact therefore
  makes the record stale and blocks until the parity suite is re-run
  (`pytest tests/test_playbook_shadow_parity.py --parity-record`).
- Rollback ready:
- Cutover eligible:
- Blocking reasons:

## Signature

Approved for cutover by: ____________________

Date: ____________________

Commit SHA: ____________________
