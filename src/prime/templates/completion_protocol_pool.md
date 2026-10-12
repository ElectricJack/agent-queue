This is a pool session. Close the held task and inspect the next-claim result:

    aq task close {task_id} --outcome pass|fail --summary "..." --claim-next --wait 60

On claimed, run `aq prime`. On no_ready_work/claim_conflict, retry
`aq task claim --next --wait 60`.
On prepare_failed/not_admissible, respect retry_after and the returned instructions.
On a repeated identical refusal (or refusal_count >= 2), report the blocker ONCE to
the project supervisor. If idle with no held claim or preparation, run
`aq session drain-ack` and exit. If preparation is retained, preserve it and end the
turn awaiting the supervisor's inbox delivery. With an active claim, use ONE durable
message wait for the reply using its thread/cursor. Do not poll claims or inboxes,
reread unchanged context, or repeat the report. A drain_requested carrying blocker
instructions also requires that report.
On daemon_unreachable / exit 3, keep retrying; never start the daemon.
On drain_requested/session_exhausted, exit; do not claim again or send /clear.
AQ normally retires context after one task; only operator policy changes that.
`.aq/claim.json` proves task/session/claim_epoch identity and supplies mutator epochs.
On stale_claim, stop work and inspect identity; never retry mutations blindly.
After an ambiguous close, inspect claim and held-task status before retrying.
