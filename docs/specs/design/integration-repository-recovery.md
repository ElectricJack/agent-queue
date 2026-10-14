# Retained integration repository recovery

The train's retained clones and delivery observers enable `core.fsync=committed`
and `core.fsyncMethod=fsync`. Git's normal default excludes loose objects from
fsync; an unclean system shutdown can therefore lose recent object data. A
cancelled Git result command must kill and reap its subprocess before returning.
These are durability fixes, not proof of the cause of the October 14 incident.

On an empty loose-object fetch/snapshot failure, quarantine only regular,
zero-byte, correctly named loose objects in the isolated retained clone. Hold
the repository transaction across quarantine, authenticated full refetch and
`git fsck --full`. Refetch must disable negotiation against damaged local tips,
including in the staged GitHub authentication path. Preserve local refs,
candidates, nonempty objects and working files. Never repair a worker worktree,
rewrite remote refs, or treat failed validation as delivery evidence. Retry once
in the current snapshot; a durable pending marker resumes interrupted recovery
on the next visit. Keep quarantine evidence and log the result. Unrecoverable
local-only objects require operator recovery, rather than destructive re-cloning.

The daemon train command checks sealed, open batches for 60 minutes without
progress, independently of successful snapshot acquisition. Retries and changing
errors do not count as progress. Paused/aborted/promoted batches are excluded;
active repairs are aged by their latest durable update. One durable incident and
supervisor inbox notice per batch are written atomically through the command
handler. The existing escalation outbox delivers the incident to Discord with
its normal retry and deduplication guarantees. A restart must not duplicate it.
Alerts never alter batch intent, approval, validation or publication gates.
