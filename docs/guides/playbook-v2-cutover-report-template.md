# Playbook V2 cutover report

Generate the current evidence with `aq playbook cutover-report --json`; paste
the result below without editing hashes or counters. A non-empty
`blocking_reasons` list means cutover is not approved.

A non-empty `evidence_errors` list means the daemon could not read one of the
sources it weighs — an unread source is never rendered as a clean one, so each
entry is also a blocking reason and the section it fed is marked
`unavailable: true`. Fix the read and regenerate; do not sign a report that did
not see the whole fleet.

## Evidence

- Generated at:
- Active contract fingerprint:
- Enabled artifacts (playbook, scope, artifact SHA-256, source SHA-256, health):
- Unresolved migration entries:
- Acknowledged disabled playbooks:
- Pending events (total, oldest age, by playbook, unavailable):
- Active V1 runs (running, paused, oldest age, unavailable):
- Evidence sources that could not be read (must be empty):
- Shadow parity (observations, identical, expected, unexplained, report path):
- Rollback ready:
- Cutover eligible:
- Blocking reasons:

## Signature

Approved for cutover by: ____________________

Date: ____________________

Commit SHA: ____________________
