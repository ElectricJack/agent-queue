# Completed Git-first container checkpoint recovery

`integration_recover_completed_container` repairs only the checkpoint of an
already COMPLETED container whose latest accepted passing completion names its
exact published final source. It is the historical-row counterpart of producer
checkpointing at close. Recovery is an operator or live named supervisor control,
using the existing integration recovery authority and project scope.

The default preview and explicit apply both verify Git-first active, designated
project/repository and exact branch, live materialized origin, completed container
and completed children, latest exact pass completion identities and source commits,
no live claim, foreign owner, pending handoff, lease holder or parent episode.
Every child source, the origin base and old checkpoint must be ancestors of the
final source. The canonical retained repository must have the configured origin;
authenticated current remote must equal the exact completion source. A released
train fence or the container's own idle reservation may remain; recovery never
changes it.

Snapshot all admission-relevant database identities before Git verification.
Apply pins the preview's completion ID, head, generation, checkpoint version,
origin digest and complete snapshot digest. Under the retained repository
transaction, freeze completion append/update for the final proof (completion
records have soft references, so task locks alone cannot prevent newer rows),
then acquire the branch owner lock, project hierarchy lock and row locks.
Recheck the complete snapshot, canonical repository and authenticated remote.
Only then CAS checkpoint SHA/version/update time, preserving generation and every
other checkpoint field. Insert the attributable before/after audit receipt in
the same transaction. Refusals and previews write no database state. Git reads
may initialize/fetch the ordinary retained object cache; no branch is published.

An identical apply after success verifies current provenance and returns the
same durable receipt without a second write. The receipt binds the operator,
reason, repository, branch, origin digest, completion, child sources, owner fence,
before/after checkpoint and snapshot identities, exact remote head/tree and time.
Any intervening identity change refuses the repeat. Without a matching recovery
receipt, an already-current checkpoint is not a recovery success.

Recovery records no completion, delivery, CI or review evidence. Normal admission
still requires exact-source review and hosted checks, and normal delivery still
checks its candidate. No live task, child, owner, origin, ref or target is rewritten.

## Operator procedure

Run from an authorized operator checkout with this command installed, or use the
same CommandHandler command under an authorized live supervisor. Worker tokens
cannot operate on the affected historical row. Source publication alone does not
authorize installing this change or applying recovery.

For the reported F2 row, preview without changing state:

```bash
aq integration recover-completed-container bright-grove-21.15 \
  --project agent-queue --repository <designated-repository-id> \
  --branch aq/bright-grove-21.15 --json
```

Inspect the native preview. In particular, completion must still be
`f3c3df91-d27f-4c7c-98e2-d8409fc0f9da`, head must still be
`72551994593615bd31f8d9305c3531443932a2ca`, old head must still be
`4b6157913479cd1dcedac1672edb50f9a69a1ff8`, and generation must still be 3.
Use the actual designated repository ID and all other identities returned by
that fresh preview. Changed values require investigation, not substituted pins.

```bash
aq integration recover-completed-container bright-grove-21.15 \
  --project agent-queue --repository <same-designated-repository-id> \
  --branch aq/bright-grove-21.15 --apply \
  --head 72551994593615bd31f8d9305c3531443932a2ca \
  --completion f3c3df91-d27f-4c7c-98e2-d8409fc0f9da --generation 3 \
  --version <preview-version> --origin-sha256 <preview-origin-sha256> \
  --snapshot-sha256 <preview-snapshot-sha256> \
  --reason '<operator-approved checkpoint provenance repair reason>' --json
```

Keep the `integration.completed_container_checkpoint_recovered` receipt, verify
normal exact-source review/hosted CI and subsequent train admission/delivery
receipts separately. `bright-grove-21.16` stays PAUSED under its existing exact
725 source-delivery hold until the supervisor evaluates that independent proof.
This command never resumes F3 or restores its removed container dependency.
