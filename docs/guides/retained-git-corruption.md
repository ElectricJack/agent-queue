# Diagnosing retained Git corruption

This procedure diagnoses an incident and defines prerequisites for recovery.
There is currently no supported live retained-cache object repair command.
Do not remove/reclone a live store, delete loose objects, repoint refs, or use a
shell restoration to bypass CommandHandler and writer authority.

The `crisp-crest-36` incident recorded 51 zero-byte loose objects in the managed
development repository. Nine hash-verified compressed bodies were recovered from
the designated local repository; 42 were unavailable there. These counts describe
the captured evidence, not current live state or complete required reachability.
The incident evidence is under the operator vault's project notes:
`notes/evidence/retained-git-corruption-1791988496819898674/`.

## Source diagnosis

The old `DevelopmentRepository` name is now `DevelopmentPrimitives.store` in
`src/integration/development.py`. It creates and fetches the retained checkout.
`DeliveryObserver` normally has its own `observer` checkout and fetch lock;
the train also resolves retained repositories through `repository_git.py` and
`development_repository`. Confirm the actual path in each failing observation.
Candidate construction, cleanup, exact imports and attached worktrees can write
objects or refs independently of an observer fetch.

Authenticated clone staging is renamed into place only after acquisition.
Incremental fetch staging lends the destination's objects through a read-only
alternate and creates negotiation refs in the temporary repository. It imports
objects back through Git, rather than truncating destination loose files in
Python. No inspected Python path establishes which writer created the empty
objects. Cancellation gaps and discarded diagnostics are confirmed software
defects, not proof of the incident's cause. An accompanying credential-broker
`requested=False` is not evidence that a credential expired.

The native lender gate now runs full `fsck` and refuses any integrity diagnostic,
including a contradictory zero exit. It checks blobs as well as connectivity,
and retains unavailable sources. It never repairs the original store. Full checks
share the existing acquisition deadline; an oversized or slow store can remain
unavailable on timeout without extending writer budgets.

## Preserve and inspect

Through the incident's authorized read-only evidence capture, record the exact
project/designated repository binding, managed path/common directory identity,
target and generation, root/repair owners and fence tokens, and pending frozen
inputs. Capture all refs (not only remote branches), symbolic HEAD, packed refs,
attached worktrees and relevant reflogs. Preserve the entire original store,
including unreachable/nonremote objects and audit material. A directory copy
made while writers are active is evidence, not a consistent recovery image.

For an already isolated disposable copy, these Git probes are useful:

```bash
git --no-replace-objects -C COPY for-each-ref --format='%(refname) %(objectname)'
git --no-replace-objects -C COPY fsck --full --no-dangling --no-reflogs --no-progress
```

Record exit status **and** stdout/stderr. `--connectivity-only` does not validate
blob bodies. A zero exit with error evidence does not pass. Never include network
credentials, environment dumps or authenticated transport configuration in the
artifact. Evidence must distinguish expected missing objects from unreadable,
corrupt or otherwise unknown objects.

## Stage recovery, then prove authority

1. Retain the source inventory and original compressed bytes. For each candidate
   body, decompress its Git header and body; verify the declared type and size,
   and hash the complete `type size\0body` against the exact expected Git OID.
   A filename or a successful `rev-parse` does not verify content.
2. Restore only into a separate disposable copy. Preserve every captured ref and
   every unique local/unpublished source, plus the explicit pending source/base
   OIDs, provenance, receipts and checkpoint history. A remote-only clone cannot
   prove this. Keep unresolved objects and unknown reachability explicitly listed;
   the nine recovered bodies alone do not establish recovery.
3. Fully verify the staged object graph, every required ref's reachability and
   its expected OID, and all explicit pending roots. Remove no evidence to make
   the check pass. Prove the stage has no dependency on temporary alternates.
   Restarting an interrupted staging attempt uses the same captured source
   identity; changed refs or missing proof require a new inventory.
4. Before any live application, require a supported CommandHandler operation
   with durable intent/audit and **all-writer** exclusion. Neither the existing
   publisher advisory/engine guard, the manager's in-process repository lock nor
   `DeliveryObserver._fetch_lock` covers every actual writer. Merely holding all
   three has not proved exclusion of external/attached writers. No current
   command supplies this authority, so this step remains refused.
5. A future applying operation must re-read project/designated-repo/binding,
   generation/snapshot, owner/fence, directory identity and ref/input inventory
   under that exclusion, before and after application. Any drift refuses the
   operation. It must preserve original evidence, recover interruption using its
   durable operation identity, and invalidate only observations of the affected
   store after verified readback. Failed or ambiguous recovery remains unknown.

The owning incident escalation handles live execution and its runtime receipts.
Leave the existing merge owner, frozen inputs, repair budget and target intact;
independent healthy repositories continue their existing work. Do not create a
second merge coordinator or manufacture a delivery receipt to clear this fault.

## Regression evidence

`tests/test_git_app_auth.py` exercises empty, truncated and missing bodies in a
local-only retained source, origin and exact fetch refusal without destination
changes, contradictory zero-exit diagnostics, an unreferenced corrupt blob,
and complete versus incomplete restoration in a disposable staging copy.
`tests/test_git_manager_async.py` cancels both local runners during startup and
execution, and admits the next writer only after the cancelled child is reaped.
`tests/test_development_integration.py` uses disposable Git and PostgreSQL to
verify unknown delivery, unchanged frozen source/checkpoint/owner/repair and
completion records, and independent healthy-store progress. Existing snapshot
tests cover project/repository/target/generation and freshness checks.

These are source-level regressions, not evidence of live cache recovery or
runtime writer exclusion. Claims, hierarchy and publication policy are unchanged;
the swarm end-to-end reset is outside this change's validation scope.
