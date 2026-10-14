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
These checks use the existing acquisition deadline. No increased budget, global
lock or persistent integrity cache is introduced. Full verification has a cost
proportional to the retained object store; timeout leaves that observation
unavailable. Independent stores retain their existing concurrency.

Local Git subprocesses used by creation and result-returning writers must retain
their handles through cancellation during spawn, and terminate/reap their process
groups before cancellation or timeout releases the caller's exclusion. This
closes a writer-lifetime gap; it does not identify the cause of a past corruption.

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
