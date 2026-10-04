# Green no-op repair settlement

A repair delegate that closes PASS has completed an attempt, even if it produces
no new commit. Record that attempt once per delegate claim in the stage dossier;
CI attempts remain separately recorded. Replaying close, dispatch, expiry or
recovery must not count the delegate twice.

Before dispatch or expiry interprets a completed delegate as no progress, read
trusted CI for the current canonical parent generation/head or batch revision/head.
The frozen producer and check-set version must match, every required check must
be successful, and newer red, cancelled, missing or infrastructure evidence must
not be hidden by an older success. Parent evidence may span multiple workflows.
Current green parent stages pass and return to aggregate verification; root
stages retain their ordinary completion/promotion handoff. Neither path allocates
a successor repair or produces a repair-no-progress incident.

`aq integration reevaluate-repair OPERATION_ID` is an integration operator control
available to the live project supervisor and local operator. It defaults to a
read-only dry run against daemon-recorded CI, showing the exact operation,
episode, generation, stage, head and successor fence plus a snapshot digest and
an apply command. The digest binds the exact close audit, current CI and readiness
proof as well as the operation and stage.
Apply requires those previewed identities and re-proves them under the project
lock, including a fresh remote-head observation. It may settle an escalated no-progress
stage whose delegate completed. It preserves attempts, deadlines, original
incidents and human decisions. It refuses live or uncertain writers, unresolved
ref/conflict writes, manual holds, open human gates, stale subjects and incomplete
or untrusted CI. A qualifying no-op requires no introduced commits (the cumulative
dossier minus the preceding stage), an empty PASS completion, a complete fenced
delegate-close event and the exact accepted-close identity for a live task.
Every delegate in the ladder must be free of live sessions, workspace locks,
manual holds and open gates. Replays of a settled stage are idempotent. It never fabricates a
CI success or grants new writer authority.

Both reported parents retain stale leaf claims from drained pool sessions and are
unassigned `IN_PROGRESS` aggregates. Recovery may release that claim through the
normal claim-release API into `PAUSED` collection, then require a fresh aggregate
verifier. Preview names `release_stale_parent_claim_to_paused_collection`. The
proof requires one claiming session, stopped state and desired state, an exact
instance confirmed stopped by its provider, matching claim epoch, no workspace
or lock, all terminal children, normal aggregate readiness, and a detached
collector fence belonging to this operation or parent. Apply rechecks these facts
under the hierarchy lock. It refuses uncertain or changed facts without mutation.

Regression checks cover unchanged green heads at stages 1 and 2 (the two reported
live failures), partial/multi-workflow evidence, stale or untrusted evidence,
newer failure, delegate attempt replay and operator authorization. The supervisor
owns live verification after deployment, using:

```
aq integration reevaluate-repair a5595725-c859-4584-8fe7-290a196b7854
aq integration reevaluate-repair 891e4e32-c094-4ebd-8774-7e079677b9ca
```

The supervisor reviews the diff and each preview before running its exact apply
command. Worker scope cannot perform these live controls. Verification concerns operations
a5595725-c859-4584-8fe7-290a196b7854 and
891e4e32-c094-4ebd-8774-7e079677b9ca after deployment.
