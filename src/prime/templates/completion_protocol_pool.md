This is a pool session. Close the held task and inspect the next-claim result:

    aq task close {task_id} --outcome pass|fail --summary "..." --claim-next --wait 60

On claimed, run `aq prime`. On no_ready_work/claim_conflict, retry
`aq task claim --next --wait 60`. On not_admissible, wait as instructed and retry.
On daemon_unreachable / exit 3, keep retrying; never start the daemon.
On drain_requested/session_exhausted, exit; do not claim again or send /clear.
AQ normally retires context after one task; only operator policy changes that.
`.aq/claim.json` proves task/session/claim_epoch identity and supplies mutator epochs.
On stale_claim, stop work and inspect identity; never retry mutations blindly.
After an ambiguous close, inspect claim and held-task status before retrying.
