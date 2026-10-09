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

## Daily backstop

While Git-first integration is active, the orchestrator runs the backstop once
per UTC day for every registered project, including paused and archived projects.
Persisted daily project markers survive daemon restarts. A failed supervisor
report leaves that project unmarked so reporting can retry without rerunning
projects whose reports were accepted.

The backstop replays landing cleanup and recovers missed terminal/abandoned
retirement decisions through `BranchRetirementService`, including accepted
archives whose branch retirement was missed. It uses that service's
durable SHA audit and verified backup bundles for unique abandoned work. Ordinary
completed work without a landing proof stays held. Live, paused or waiting tasks
never become retirement candidates merely because an old abandonment marker exists.

Registered workspace, repository base, integration and publisher checkout common
directories are visited once. The ordinary sweep deletes only tips Git proves
contained or cleanly patch-equivalent to fetched `dev`, `main` or the repository
default branch. Each deletion records its exact SHA and proof in the project's
daily TSV before mutation, and uses an expected-old-SHA lease. Before each delete,
the sweep holds the shared per-ref exclusion, locks associated task rows and
rechecks live references, unreleased owners, configured flow targets and attached
worktrees. Unknown repository identities are skipped. Unique unmerged refs and
stashes remain intact.

Legacy provenance branches use the existing verified copy-before-delete migration
under those same live-reference guards, with a pre-delete audit entry. Invalid or
conflicting provenance remains blocked. The operator cleanup script shares the
Git proof and per-ref safety implementation; its default remains a dry run.

Each project's supervisor receives its own inbox report through `CommandHandler`,
with local/remote deletion and hold counts, retirement and migration outcomes,
unique refs, stash/registration counts and failures. Cleanup failures in one project
do not suppress another project's report.
