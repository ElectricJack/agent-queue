# Historical parked operation retirement

A LOCAL operator may use `integration_retire_legacy_park` to withdraw an old
`development.operation` record still parked after its publisher was retired.
Preview and apply select the project, operation and every manifest task explicitly.
Apply requires the exact previewed event id and payload SHA256. Each selected task
must be terminal or archived in that project. Missing, malformed, changed,
unselected or active inputs refuse without writes. Repository engine and hierarchy
exclusions serialize the proof; live sessions, unresolved claims, locked workspaces,
attached branch writers, running subjects, reserved ref mutations, unsettled
promotion intents and project leases refuse retirement.

Apply appends a `cancelled` revision with the operator's explicit abandonment
reason and original event identity. It never claims delivery or changes task
completions, prior events, Git refs, owner reservations or cleanup fences. Repeating
an exact successful request is idempotent. The normal archive guard can then
archive those terminal sources with explicit undelivered abandonment. The new
train does not execute the retired publisher.
