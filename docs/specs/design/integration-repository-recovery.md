# Retained integration repository recovery

The retained development checkout, delivery observer and repository reader enable
`core.fsync=committed` and `core.fsyncMethod=fsync`. Local Git cancellation kills
and reaps the subprocess before the repository transaction releases. These are
object durability fixes, not proof of the October 14 incident's cause.

The approved `rev-swift-flare` revision 2 recovery runs through the existing
integration acquisition seam and CommandHandler. Full `fsck` diagnoses the actual
managed cache. Ordinary network/authentication failure with clean objects creates
no recovery incident. Any unexpected integrity diagnostic fails the check, even
with exit status zero. All acquisition, diagnosis, refetch, publication and final
checking share the existing Git acquisition deadline; recovery never grants a
new work-stage deadline.

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
Failure keeps the backup and admission hold and atomically records one coalesced
supervisor/escalation notice. The incident's bounded evidence updates on later
failure without repeating the notice. A restart retains both the filesystem move
ledger and PostgreSQL admission state. No migration or operator database write is
required to install the implementation.

The train separately checks sealed open batches for 60 minutes without progress.
Its stall incident mechanism remains independent of cache recovery and never
changes batch intent, validation or publication gates.
