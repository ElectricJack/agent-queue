# Operational retention and scheduler load

Task calm-grove-13, supervisor policy confirmed 2026-10-22.

Operational retention runs hourly in a worker thread with its own one-connection
PostgreSQL pool. Each pass processes at most 1000 rows per table. An operator can
run `aq system operational-retention --dry-run` to count eligible rows without
writing files or deleting data. All mutations enter through CommandHandler.

Before deletion, append full rows to private, fsynced gzip JSONL archives under
`<data_dir>/backups/<table>-archive-<UTC-date>.jsonl.gz`. Binary values use tagged
base64. Archive writes happen without an open database transaction. Deletion
rechecks eligibility and PostgreSQL xmin, so concurrent changes survive. A crash
may leave duplicate archived records; it must never leave deleted unarchived data.

Horizons are 30 days for token_ledger and events; 14 days for dispatched
wait_source_events at or below the source listener cursor. Missing cursors retain
all events. Timer cursors do not consume source events. Transcript usage maxima
are retained: they prevent duplicate billing and their compaction is separate
work (bright-cascade-60).

Private values expire only outside live runs, or when their run completed over
30 days ago. Unexpired namespace/snapshot values survive. Receipts require an
old terminal run. Runs with live parents, surviving children, workflow/job
references, private values or receipts survive. Run waits and assignment-route
records are archived before their parent run, avoiding unarchived cascades.
Values, receipts, auxiliary children, then runs are processed in that order.
Existing artifact file collection remains independent; it cannot delete run or
receipt rows ahead of this archiver.

Scheduler resource observations must reuse their caller's connection under the
graph fence. Periodic reconciliation may coalesce event hints, but missed events
and time-based constraints require a bounded periodic recovery scan. Admission
checks remain authoritative and must not use cached readiness as permission.

The scheduler preflights delivery observations once per project outside the
registration transaction and revalidates them on its existing connection. It
reconciles dirty projects on task/session/workspace/configuration events, with a
60-second recovery scan. Integration target discovery uses a 30-second cache
invalidated by train wake events; visits still prove current eligibility. Held
legacy bridge attempts retry at most once a minute. Worktree pruning and retired
slot maintenance run as a serialized background task joined on shutdown.

Transcript readers already parse in worker threads; this change preserves their
existing accounting and anti-replay behavior. Project, profile, archive and task
list hydration releases owned database transactions before constructing Python
objects. Caller-supplied transactions retain their original ownership semantics.

Deployment acceptance requires observing scheduler cycle and pool-wait warnings
under normal operator load. Unit tests and the isolated swarm smoke exercise
correctness but cannot substitute for that production observation.
