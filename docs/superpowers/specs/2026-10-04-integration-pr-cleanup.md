# Integration owns pull request retirement

Terminal train batches retain restartable PR cleanup independently of delivery.
An abort materializes only its published audit PRs, deduplicated across candidate
revisions, in the abort transaction. Cleanup comments identify the batch, aborted
outcome and recorded reason. Source PRs, refs and retained work stay available.
Expected repository and head identities remain binding. Historical aborted
batches can materialize missing cleanup through retry-cleanup and reconciliation.
Terminal audit PR retirement remains independent of root engine ownership; source
PR/ref cleanup and active batches keep their existing writer ownership guards.

The integration service periodically inventories every open same-repository
`aq/*` PR using paginated GitHub reads. Bounded passes call the existing
close-delivered-pr command, preview then apply with the observed head. Its
ancestry, patch-equivalence and content-equivalence proofs remain authoritative;
an undelivered, moved or unreadable PR stays open. Errors isolate individual PRs
and repositories, and a later inventory retries failed closures.

Successful default-branch adoption retires the tracked task PRs. Delivered-child
adoption also retires the obsolete parent aggregate's PR, with a settlement
comment rather than a claim that its aggregate landed. Explicit obsolete closes
retire superseded PRs through their existing retryable cleanup marker. Closures
bind the designated repository, canonical task branch and observed PR head,
recheck task settlement and writers, and preserve idempotent comments and audit.
Failed GitHub cleanup does not undo an already committed settlement.

Doctor and stall.sweep report open `aq/*` PRs older than 24 hours with neither a
nonterminal task, live writer/reservation nor live train owning that branch/PR.
Age is measured from PR creation. Reporting is read-only and includes PR URL,
branch and age; failure to inventory a repository is visible.

Regression checks cover aborted cleanup/retry/replay/head changes, periodic
delivery proof and undelivered work, adoption and supersession cleanup failure
and retry, and stale orphan reporting with live-owner exclusions.
