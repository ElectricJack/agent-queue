# Retained integration repository recovery

Retained integration stores always bind `origin` to the configured repository URL,
including stores initialized for branch-discard backups. Acquisition and the daily
branch backstop restore a missing or empty origin on a known retained-store path
from that repository's configuration. A conflicting nonempty origin is never
replaced; acquisition refuses it and the backstop reports it without sweeping it.
Unregistered checkouts do not acquire a remote from a guessed project identity.

The retained development checkout, delivery observer and repository reader enable
`core.fsync=committed` and `core.fsyncMethod=fsync`. Local Git cancellation kills
and reaps the subprocess before the repository transaction releases. These are
object durability fixes, not proof of the October 14 incident's cause.

No write to a retained store spawns automatic maintenance. The store config and every
isolated import set `gc.auto=0`, `gc.autoDetach=false` and `maintenance.auto=false`:
under git 2.43 a detached auto-gc ran in a temporary HOME that was deleted under it,
died, and left a `gc.log` whose `fatal:` lines every later `gc --auto` repeated on
stderr with exit status zero, which the integrity check read as a failed fetch and
wedged the cache. Maintenance instead runs explicitly in the foreground under the
repository transaction after a successful acquisition (`gc --auto` with a real
threshold, never detached); a successful run removes a stale `gc.log`. Only the
stale-report block itself is forgiven on a zero exit; any other `error:`/`fatal:` line
still fails.

The approved `rev-swift-flare` revision 2 recovery runs through the existing
integration acquisition seam and CommandHandler. Full `fsck` diagnoses the actual
managed cache. Ordinary network/authentication failure with clean objects creates
no recovery incident and spends no attempt; when a clean fsck finds an earlier
incident's store already clean with nothing to move, that incident closes as recovered
and its escalation resolves. Any unexpected integrity diagnostic fails the check, even
with exit status zero. A full fsck has a five-minute command budget, including the
supervisor's clear check. Acquisition reserves four such checks in addition to the
ordinary Git acquisition budget, covering diagnosis, pre-repair verification and
both final checks. All acquisition, diagnosis, refetch, publication and final
checking share that single bounded deadline; recovery never grants a new
work-stage deadline or renews the acquisition deadline between checks.

Each incident holds admission of **new** batches for its project. CommandHandler
persists its identity, attempts, diagnostics and move list in the existing
`system_config` ledger. New batch creation shares the incident writer's project
row lock. Other projects and exact replays of already frozen batches are unaffected.
The hold is independent of project status, operator decisions and batch intent.

Under the normal repository transaction, move only regular, correctly named
zero-byte or Git-diagnosed corrupt loose objects into a timestamped backup outside
`objects`. Preserve their bytes and persist each move intent before renaming.
The backup's `manifest.json` records cache/project/repository/incident identity,
attempts, original and backup paths/OIDs, and scrubbed before/after fsck diagnostics.
The current journal and pending marker remain in `aq-empty-object-backups` inside
the clone's Git directory. Interrupted move intents are reconciled on restart.
Packs, unsafe paths/types and unidentified corruption require supervisor intervention;
healthy bodies, local refs, source refs and frozen records are never rewritten.

Fully refetch origin using the configured transport and the existing refetch mode
without negotiation against damaged tips. Fully verify all object bodies before
publishing readable local-only `refs/heads/aq/` branches through `apush_new_refs`.
Every new head is leased to absence and read back; existing remote heads and
non-AQ heads are never published by recovery. Missing local-only history or
rejected/ambiguous publication fails recovery. Full checking after publication is:

```bash
git --no-replace-objects -C CACHE fsck --full --no-dangling --no-reflogs --no-progress
```

Success requires zero exit and no integrity diagnostic, plus successful guarded
local-only publication. Clear only that incident's admission hold. The original
caller resumes its existing batch/snapshot flow, with its frozen inputs, current
owner, validation, leases and publication gates. Recovery does not prove delivery.

Allow at most two attempts per incident, counting cancellation and interrupted
attempts. Subsequent owner visits use the same incident and remaining budget.
After exhaustion, repeated visits cannot start a fresh incident or retry refetch.
Failure keeps the backup and admission hold and records its evidence. Only failure
of the final attempt atomically records one coalesced supervisor/escalation notice;
a retryable failure never pages the human. The incident's bounded evidence updates
on later failure without repeating the notice. Every attempt records its own error
in its check, and the exhausted refusal names the last one. A restart retains both
the filesystem move ledger and PostgreSQL admission state. No migration or operator
database write is required to install the implementation.

An exhausted (or failed) incident is lifted only by the project-scoped supervisor and
operator control `integration_clear_retained_recovery` (`aq integration
clear-retained-recovery --project-id <p> [--incident-id <id>]`). Under the cache's
repository transaction it reruns the full fsck above. A clean cache marks the local
journal and the PostgreSQL ledger recovered with the clearing principal, removes the
pending marker and resolves the escalation (`cleared`); a dirty one changes nothing
(`not_clean`). It moves, refetches and publishes nothing, and never resets an attempt
budget in place.

The train separately checks sealed open batches for 60 minutes without progress.
Its stall incident mechanism remains independent of cache recovery and never
changes batch intent, validation or publication gates.

Snapshot target absence is `target_ref_missing`, distinct from an unavailable
object store. After an object-integrity acquisition refusal, an authorized remote-ref
read may establish that the requested target no longer exists. That answer proves
no delivery and supplies no fetched repository inventory: another target must
retain the acquisition failure rather than inherit an absence verdict. Failed
remote reads never establish absence, and acquisition diagnostics remain available.

Discovery omits a completed container's absent target after a fresh remote
observation, including routes from its historical children. Open batches retain
their targets for recovery; active containers and unreadable refs retain their
diagnostics. Once discovery retires a target, the train drops its finished visit
from status, preserving any running visit until it settles. Retirement changes
no task, branch, batch or delivery evidence.
