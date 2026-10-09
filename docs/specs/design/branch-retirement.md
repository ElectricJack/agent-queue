# Explicit branch retirement

An archive, abandon, cancel, failed close, superseded task or aborted batch
retires its private branches. Protected branches (`main`, `dev`, `staging`,
the repository default, `gh-pages` and every configured promotion target)
remain. Aborting a batch retires only its candidate; its sources still own
their work. A retry, provider failure or temporary pause is not abandonment.

The lifecycle transaction records a `branch_retirements` intent for each named
branch, with no task foreign key so it survives archive/delete. Requests are
idempotent within a decision. The daemon drains these intents after restart.
Batch aborts and supersedes queue both private candidates in that same transaction.
Before deleting an observed local or remote tip it commits its name, SHA,
location and verified backup bundle to the audit row. Unmerged commits remain
restorable from bundles, rather than surviving as branches.

Deletion holds the existing per-ref exclusion, rechecks task generation,
live sessions, owners, active subjects/batches and attached worktrees, and
uses the observed SHA as an exact lease. Reopened or re-claimed work invalidates
an older decision. A moved head is a conflict; transport failures and temporarily
held branches retry with backoff. The audit is retained after successful removal.
Local removal visits registered AQ checkout common directories for the same
repository and the retained integration and publisher stores. It never detaches
another writer. It removes audited `origin` tracking refs alongside local heads,
so deleted remote branches do not remain visible as stale local observations.

Explicit materialized-origin deletes use the same retirement mechanism. Ordinary
delete with `branches=keep` remains an explicit preservation choice. Archive's
existing delivery and live-session admission guards remain required; admitted
archives now retire their branches instead of leaving orphan refs indefinitely.

The earlier `branch_deletion_audit` schema and revision remain as history.
Pending, held or failed rows are imported once into retirement intents; their recorded
SHA stays a deletion precondition, and the old audit is settled on confirmation.
