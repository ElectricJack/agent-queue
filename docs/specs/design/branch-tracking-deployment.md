# Branch tracking for a source installation

Status: implementation specification, 2026-10-12 (nimble-grove-32).

An operator may set `deploy.track_branch` to a branch name (for example `dev`).
Null disables automatic deployment. `deploy.poll_interval_seconds` defaults to
300 and must be at least 60. This is installation configuration, hot reloadable,
independent of project integration and worker routing. It conflicts with tag
selection. The installation must already be on that branch with a matching
upstream; automation never switches branches or overwrites local work.

The orchestrator schedules a background check at this cadence through
CommandHandler's `update_and_restart`; slow fetch/build work never blocks the
scheduling cycle. Checks refuse dirty tracked files, detached/tag deployments,
missing or mismatched upstreams, local/divergent history and invalid migration
graphs (exactly one target Alembic head). Shallow history is expanded before
proving ancestry. The updater pins the fetched commit, rechecks HEAD and the
clean tree under the update lock, and only fast-forwards.

A detached operator updater outlives the daemon, with a scrubbed environment.
It uses the existing update transaction: pre-migration backup, stop while
preserving all agent sessions, fresh-process dependency install and dashboard
build when build inputs changed, restart, daemon and dashboard health checks,
and restoration of the previous code/bundle/start on failure. A database that
may already have migrated keeps the newer code and names the backup for operator
recovery; automated code rollback never downgrades or restores the database.
This sets a persistent recovery hold, independent of history retention, which
blocks every automatic target. An operator restores the installation and runs
`aq update --yes --resume-after-recovery`; health, readiness and the installed
dashboard must pass before releasing the hold. Automatic updates require backup.

A private, bounded deployment history survives restart and is visible on System
Config in the dashboard. It records time, branch, previous/target commit, started,
updated, refused, failed, rolled-back, recovery-required or recovered outcome,
and sanitized diagnostics (including backup path and daemon log remediation).
No-op checks do not flood the history. A failed automatic target remains held
until a newer commit arrives or an operator retries `aq update --yes`.
Disabling tracking stops new checks; it does not cancel an update already begun.
Workers remain unable to update, restart or migrate the operator installation.
