# Retained Git object integrity

Task: `crisp-crest-36`. Scope: native detection and cancellation safety.

Before an isolated authenticated fetch advertises retained tips as held, the
credential-free Git runner must verify their lender with full object checking.
A connectivity-only walk is insufficient: it can accept an empty or truncated
blob. Exit status zero accompanied by integrity diagnostics is unavailable,
never success. Malformed tip inventories and failed tip naming are unavailable.
Origin fetches also verify local-only refs when no origin tip can be advertised.
Tip naming is transactional in the disposable import repository. The local
exact-commit shortcut must walk the requested history without excluding seeded
refs; an unreferenced commit needs the same full-body check before local success.
Default isolated origin and exact acquisitions give retained verification its
own bounded 300-second phase before lending objects. This phase includes the
store-lock wait and all retries, and preserves the transfer's remaining budget
instead of giving full verification only that remainder. An explicit exact-fetch
timeout remains an overall cap; enclosing visit cancellation still propagates.
No global lock or persistent integrity cache is introduced. Full verification
has a cost proportional to the retained object store; timeout leaves that
observation unavailable. Independent stores retain their existing concurrency.

A failed full verification is retried up to three times with one-, two- and
four-second backoff within the same integrity deadline (including lock waits
and backoff). Verification and GitManager's foreground `gc`, `repack` and
`maintenance` share per-common-store exclusion, including linked worktrees and
commands inside repository transactions. Independent Git processes do not take
this in-process lock; their pack/ref changes can still invalidate an observation.
Only a subsequent full clean check authorizes use; persistent errors remain
unavailable. Diagnostics retain safe error categories and failing object IDs,
without emitting untrusted paths, ref names or credentials. Object fingerprints
for linked gitdirs resolve `commondir` so shared-store writes invalidate cached
successes, including when a write occurs during verification.

## Incremental retained-store acquisition

A clean full check is reusable in process for at most one hour. Fingerprint
scans run off the asyncio event loop. They cover object paths, sizes and inodes,
including shared `commondir` stores, and ignore Git's abandoned `tmp_pack_*` and
`tmp_obj_*` files. Temporary files are preserved for normal Git maintenance.

An isolated retained-store acquisition checks its baseline before fetching.
Every origin transport and staged local import enables `fetch.fsckObjects` to
check incoming objects. Under the existing repository transaction, a successful
fetch may extend the baseline to added objects only if every previously checked
file remains unchanged. This preserves the original full-check timestamp;
incremental traffic cannot defer the next full check indefinitely. Missing,
replaced or truncated files and expired checks still require full verification.
Failed acquisitions never extend the baseline. Incident recovery retains its
full checks and durable journal.

The isolated origin and exact-fetch seams carry this baseline forward after
successful transport, checked local import and exact ref readback. Both legs of
an exact fetch enable `fetch.fsckObjects`, just as origin fetches do. Unknown
additions still invalidate verification; only successful checked acquisitions
under the caller's repository writer exclusion may extend it.
Full-fallback exact imports use `--refetch` so a partial destination's tips do
not negotiate away promised blobs that the staged acquisition downloaded.

Admission shares a five-second snapshot-wait budget across one request's Git
observations. A slow fetch or recovery continues as the existing shared flight
under its repository exclusion. The caller receives unavailable evidence and
withholds only candidates requiring that proof; structurally independent work
can proceed. A timed-out caller neither cancels recovery nor uses stale delivery
proof. Later admissions still require a fresh observation and the existing
identity and remote-ref fences. Other delivery writers retain their normal
acquisition budgets.

Local Git subprocesses used by creation and result-returning writers must retain
their handles through cancellation during spawn, and terminate/reap their process
groups before cancellation or timeout releases the caller's exclusion. This
closes a writer-lifetime gap; it does not identify the cause of a past corruption.

## Tick-scoped observations

Train lanes dispatched by one tick share one repository acquisition, including
lanes arriving after that acquisition starts or completes. Reconciliation stages
likewise share one observation per repository in their tick. The train and Git
truth reconciler use the same GitTruth service for immutable facts and backoff.
Only work dispatched by that tick retains its scope; later ticks and publication
follow-ups enter a new scope. Each reader retains independent answer and freshness caches.
Mutable authority and remote target identity still require their existing checks
before a proof can authorize a state change.

The service dispatch encloses both train lanes and Git truth reconciliation in
one scope. Standalone ticks create their own scope; publication follow-ups
always create a new one. Reader caches and remote freshness fences remain local.
The reconciler opens the train's canonical retained `repository` checkout under
the same storage binding, rather than creating a separate reconciliation clone.
Both readers therefore reuse its TTL/fingerprint integrity baseline between
ticks as well as its snapshot within a tick. Concurrent first opens serialize
clone creation, origin validation and hardening under repository exclusion.

For these background observations, a reusable full integrity baseline allows a
fresh authenticated `ls-remote --refs` inventory before transport, under the
repository transaction. All branches, tags and provenance refs are compared
with the retained import inventory, including additions, deletions and rewinds.
Only new or changed refs travel through authenticated transport and the local
checked import, using explicit refspecs on both legs. Unrelated lane targets
share the captured inventory and acquire a moved task branch only once per tick.
Partial imports never prune other refs. Deleted branch/provenance tracking refs
are retired in a local ref transaction guarded by their observed old OIDs;
tags and objects remain retained. Deletion-only or unchanged inventories need
no transport or foreground maintenance. Changed imports retain fsckObjects and
the existing baseline-extension checks. Expired or changed integrity baselines,
recovery journals and pending markers retain full acquisition and recovery.
A failed remote probe leaves the observation unavailable.

Scheduler and pool sizing read only completed, bounded-age snapshots and start
one background refresh per repository when needed. Cold or expired snapshots
remain unavailable for that pass; they never await the shared acquisition.
Diagnostics do not start refreshes. Claims and preparation keep their ordinary
synchronous acquisition, identity checks and remote freshness fences.

Per-visit selection metrics distinguish `snapshot_acquisition`,
`snapshot_scope_wait`, `snapshot_ref_probe`, `retained_fetch`, `retained_fsck`
and `retained_maintenance`. Counters include `tick_snapshot_hits`,
`snapshot_ref_probes`, `snapshot_unchanged`, `snapshot_changed_refs`,
`snapshot_removed_refs` and `retained_fetch_starts`.

## Recovery authority

Detection is read-only with respect to the lender. It does not change refs,
loose objects, frozen batch inputs, completion provenance, checkpoints, receipts,
owners, policy or holds. An absent unreferenced object may still be acquired by
the existing exact-fetch path. A corrupt retained ref is never replaced or
silently discarded to make a fetch pass.

No cache replacement or object restoration command is defined here. An applying
repair requires a CommandHandler-owned operation that validates all of:

* the exact project, its designated repository, immutable repository binding,
  managed common Git directory and directory identity;
* the observed generation, target OID, ref inventory digest, pending frozen
  source/base OIDs, provenance, checkpoints and active root/repair owner tokens;
* exclusion of **every** actual writer, including creation, observer fetches,
  exact imports, candidate construction, cleanup and attached Git worktrees;
* complete required-ref reachability and exact object header, size and body hash
  in a separate staging store, with no dependency on disposable alternates;
* fresh before/after readback of those identities under the same exclusion.

The existing publisher advisory lock, a GitManager transaction and the observer's
in-process fetch lock are individually insufficient as a live recovery fence.
Unknown or changed authority, missing source objects, interrupted restoration or
inconsistent readback retain the original store and unavailable observations.
Any future applying seam must record durable operation intent and replay evidence
through CommandHandler; neither a successful unit test nor a timer is a receipt.
See the [incident procedure](../../guides/retained-git-corruption.md).
