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
Full verification runs as one background flight per common object store, with
its own 900-second budget (`RETAINED_FSCK_TIMEOUT_SECONDS`) that no caller's Git
deadline or cancellation shortens. This accommodates measured full checks of over
nine minutes on healthy multi-gigabyte retained stores. The budget includes the
store-lock wait and all retries. Default isolated origin and exact acquisitions
wait up to that phase before lending objects, and preserve the transfer's
remaining budget instead of giving full verification only that remainder. An
explicit exact-fetch timeout remains an overall cap; enclosing visit cancellation
still propagates to the caller but not to the shared flight (see
[Background full verification](#background-full-verification)). No persistent
integrity cache is introduced. Full verification has a cost proportional to the
retained object store; a caller that stops waiting leaves that observation
unavailable to itself, never to the flight. Independent stores retain their
existing concurrency.

Ordinary authenticated origin fetches run full verification and tip lending
before acquiring repository exclusion. Network transfer, checked local imports,
exact ref readback and baseline extension share one repository transaction.
The borrowed object store remains protected from foreground maintenance during
transfer and import. The baseline is rechecked after staging under that
transaction, so concurrent unknown writes
cannot be accepted as checked additions. Automatic maintenance is disabled on
the local imports; foreground maintenance retains its verifier exclusion.
Callers explicitly passing `lock_held=True` retain their enclosing transaction
and its stronger compound-operation guarantees. Bounded repository-lock waiters
(such as claim preparation and Git truth reconciliation) can acquire the lock
while an ordinary pull's full integrity check is running.

Slot provisioning, reset, restore, idle detachment and handoff refresh origin
before entering the shared base-repository mutex. Local ref resolution and all
checkout/holder proofs and mutations run under that mutex after refresh; a failed
refresh cannot authorize detachment. Detaching an abandoned holder preserves its
branch and commits. Its publication evidence is refreshed before the mutex,
then its live holder/binding and exact published HEAD are rechecked inside it.
A holder appearing after preflight is refused until a fresh attempt; no fetch
runs inside the mutex. These slot lifecycle paths must allow an independent bounded
epic-branch preparation while full lender verification is in progress.

Owner recovery and task-origin restoration likewise refresh origin before
entering the shared checkout mutex. They read the remote head, local refs and
worktree bindings after acquiring that mutex, retaining publication leases and
owner-fence checks. A bounded repository-mutex timeout records the holder's
elapsed ownership and coroutine await chain, without arguments or locals, so
subsequent refusals identify the remaining critical section directly.

A failed full verification is retried up to three times with one-, two- and
four-second backoff within the flight's own integrity deadline (including lock
waits and backoff). Verification and GitManager's foreground `gc`, `repack` and
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
additions are never trusted as checked: before lending, they pass the
incremental gate below or a full check. Only successful checked acquisitions
under the caller's repository writer exclusion, or the incremental gate, may
extend the baseline.
Full-fallback exact imports use `--refetch` so a partial destination's tips do
not negotiate away promised blobs that the staged acquisition downloaded.

## Background full verification

Task: `wise-nexus-76`. A 2–3 GB retained store's full fsck exceeded the ~120 s
deadline of the fetch that started it. The check was cancelled with its caller,
the cache key differed per linked worktree and a busy store never matched its
pre-check fingerprint, so nothing was recorded. Every claim preparation then
restarted the check and `owner_lease` ran out its 600 s.

- **One flight per store.** The key is the real path of the common gitdir, so
  linked worktrees share it. The flight starts in a fresh context, holds the
  store's verifier exclusion and runs with its own budget. It is not cancelled
  when a caller is.
- **Callers wait without owning.** A caller shields the flight and waits at most
  the shorter of its own deadline and its repository-lock wait limit. Claim
  preparation (five-second limit) receives `RetainedVerificationPending`, a
  `RepositoryLockTimeout`. The claim records prepare stage
  `retained_verification` and returns the retained refusal with a bounded
  `retry_after`; this does not count toward quarantine. Any other caller receives
  a Git preparation error with reason `verification_pending`. The flight continues
  in both cases, and the next attempt reuses its result.
- **Recovery preserves pending checks.** If an acquisition fails while its shared
  full check remains active, the recovery wrapper propagates that failure without
  starting a second diagnostic fsck or opening an incident. In particular, a
  claim's `RetainedVerificationPending` remains a bounded repository-wait refusal,
  rather than becoming a generic acquisition timeout. Diagnostic fsck commands,
  including incident checks, share the common-store verifier exclusion with full
  checks and maintenance, so their subprocesses cannot overlap.
- **Recording under concurrent writes.** The flight fingerprints the store before
  and after its check and records only the files that are present and unchanged in
  both. An object written mid-scan lives in a file absent from the first
  fingerprint, so it never enters the baseline and stays an unchecked addition.
- **Incremental foreground gate.** When the store has only gained files since a
  clean baseline (new loose objects, or new packs with their indexes, at most
  256 MiB together), the caller runs the gate itself, without the verifier
  exclusion. First, a full fsck of just the added files, linked into a scratch
  repository that borrows nothing else. Then a connectivity-only fsck of the whole
  store, which also verifies commit-graph and multi-pack-index. The gate fails
  over to the full flight if a baseline file has been replaced or truncated, if a
  path is unknown, if an index has no pack, if the additions exceed the cap or
  if either check fails. Removed files need no body check because connectivity
  proves what remains. A pack whose index has not been written yet is still
  arriving: Git does not use it, so it stays outside the checked inventory. Verification is never skipped, and
  connectivity-only never admits an object no full check has read. The gate keeps
  the baseline's original timestamp.
- **Refresh.** A caller that reuses a baseline more than half the TTL old starts
  a background refresh flight and proceeds on the still-valid baseline.
- **Repack carry.** Task: `sound-journey-54`. The hourly background
  `repack -a -d -k` replaces every pack with one larger than the incremental cap,
  so every repack used to leave callers waiting on a full flight of up to 900 s.
  `GitManager.arepack_retained_store` holds the verifier exclusion across the
  repack and everything after it. The baseline carries only when three things
  hold: the store matched its clean baseline exactly beforehand, the repack
  exits zero with empty stderr, and a connectivity-only fsck of the result
  passes. That fsck took 15 s on this repository's 1.9 GB store. The carried
  baseline names the new files but keeps its original timestamp, so the
  refresh still comes due on schedule and a full fsck still reads every body
  within the TTL. A repack rewrites only objects the full check has already
  read, and the connectivity check proves that nothing reachable was lost.
  Nothing carries in these cases, and the full check stays due:
  - unverified additions;
  - anything but the repack's own pack family (and derived metadata such as
    `info/packs`) added during the repack, which runs outside the repository
    transaction (`calm-lantern-36`), so another writer may add objects beside it;
  - a failed repack or a failed check;
  - a baseline replaced meanwhile.

  The exclusion is re-entrant only within its holder's context, so the repack
  passes through it. A flight's fresh context never inherits the hold.
- **Queued checks recheck.** A full flight that waited on the exclusion
  fingerprints the store once it holds that exclusion. If the store exactly
  matches a clean baseline younger than the refresh age, the flight returns
  that baseline without running fsck, so a caller that saw the store mid-repack
  reuses the carry. Refresh flights start only once a baseline reaches that
  age, so they always reread the store.

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
The reconciler joins an existing or in-flight tick snapshot before opening the
checkout or starting an observer acquisition. Successful checkout opens are
coalesced within that scope by Git manager, resolved storage path and origin
URL, so lane setup does not wait behind a peer's snapshot fetch. A later scope
revalidates the checkout; integrity checks remain with snapshot acquisition.
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

Unchanged lanes have a minimum 30-second interval between visit starts,
independently of service tick frequency. Readiness events, explicit visit requests
and changed target configuration bypass this interval. Events received during a
visit remain pending for its successor. Repository concurrency, rate-limit pauses
and publication checks still apply; lost events recover through ordinary revisits.

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

### Missing-object origin acquisition

Task `nimble-horizon-50`, supervisor decision
`msg-a5d78ac44d89465892ad1823287a5083`, permits one bounded additive import
inside an already authorized origin acquisition. The read-only verifier reports
typed missing-object evidence only when every diagnostic is either an exact
`missing <commit|tree|blob|tag> <oid>` line or a well-formed broken-link pair
whose target type and OID match one of those absences. Standalone or unexplained
broken links, errors, warnings, corruption, truncated objects and all other
diagnostics retain unavailable evidence without attempting an import.

Under common retained-store exclusion, recheck the evidence, then acquire a
complete isolated origin store without borrowing
retained objects, and prove every named object is available with its expected
type. Import only those OIDs and their reachable closure, with incoming object
checking, without changing destination refs or FETCH_HEAD. Preserve every local
and unpublished object, the checkout, saved head and saved branch identity.
Never replace the store or delete an object. A full clean destination fsck is
required before lending objects; unavailable upstream objects, transport failure
or a second integrity failure propagate to the existing workspace hold. There
is one recovery attempt per acquisition, bounded by the retained integrity
deadline, with no new workspace retry classification or relaxation of
`verify_saved_branch`.

Ordinary acquisition keeps every full integrity check and isolated network
staging outside repository exclusion, preserving the bounded lock wait of claims.
Only the additive import takes repository exclusion, followed by common-store
exclusion. Revalidate the object-store fingerprint before importing; a changed
store retains unavailable evidence. Release repository exclusion before the final
full integrity check. An explicit enclosing transaction passed with
`lock_held=True` retains its caller's existing stronger exclusion guarantee.
The recovery deadline includes lock waits, and the additive local import disables
automatic maintenance. This preserves the claim-lock guarantee of `ce8dc58cb`
under supervisor decision `msg-3a67a988804742469c386795c725cd62`.
