# Integration subject writers — primitives 11–13

Implemented for `amber-meadow-15.2` from approved `rev-agile-ridge` revision 2,
§3.4, using the contracts/schema from prerequisite `vivid-willow.1` at
`9ad7f4ead`. The approved design is in the operator vault at
`projects/agent-queue/specs/2026-10-02-integration-train-mechanism-inventory-and-simplification-can.md`.

`src/integration/writers.py` supplies `WriterPrimitives.bind(ports)` for
`writer_file`, `writer_lease`, and `writer_stop_proof`. The public command
surface and production engines retain their existing behavior. This module
adds no migration, command registration, service startup, or activation.

## Filing and budgets

All roles (`repair`, `verifier`, `source_repair`) use one filing path. Root,
parent, source-CI and development repairs differ only in subject/role/brief
inputs. A task and its immutable `writer-file:<ordinal>` action journal entry
are committed with the subject's writer/budget fields in one transaction.
The task identity is deterministic on `(subject.id, ordinal)`. Replaying the
same request returns the recorded task and original budget, even when the
caller still has its pre-filing observation. Changing a request under the
same ordinal is `configuration_blocked`, never a new clock or worker.

Filing sets a class hint, never a profile or provider. A task starts `BLOCKED`
with the existing terminal retained-handoff marker;
the lease makes it `READY` and durably queues `task.created` for mandatory
routing. The CommandHandler adapter supplies its usual `routing_policy`
callback. A new ordinal requires a proven stopped predecessor; the ladder,
budget sizes and decision to retry remain policy inputs. Existing attempts
and deadlines are preserved on every replay.

## Lease authority

The backing row is the existing repository-qualified
`integration_branch_owners` record, with its monotonic fence and `expires_at`.
The first lease is capped by the budget deadline. Replay returns that expiry
without renewing it; expired or exhausted budgets are stale. Expiry alone
never allows release or takeover of a predecessor. Ref aliases are checked
together so `candidate` cannot evade an owner of `refs/heads/candidate`.

A detached reservation belonging to the subject's domain identity can
transfer to its writer after verifying no session/workspace holds it. A
foreign, attached or unresolved reservation returns `busy(holder)`.
Released rows allocate a fresh token through the existing ownership API.

Publish adapters must use `WriterPrimitives.ownership`, the
`ExpiringBranchOwnership` port. Its `assert_current`, `attach`, and
`mutation_exclusion` checks reject a fence without an unexpired lease.
Use the mutation exclusion for the bounded journal/expected-old push boundary,
with the actual ownership state (`reserved` or `attached`). Generic legacy
`BranchOwnership` remains the feature-off behavior. This primitive does not
publish Git refs or replace the repository-wide publisher lease, trusted CI,
attestation, or journal-before-push requirements.

## Stop proof and preservation

The adapter wraps the daemon's existing `OwnerRecovery`; it does not duplicate
its Git snapshots, preservation pushes, workspace/claim cleanup, or audit.
The wrapper pins the exact owner/fence, treats missing session/provider/
workspace evidence as `unknown`, and never treats an expired TTL as proof of
death. The provider must freshly confirm a recorded stopped session.

At release, the wrapper augments the existing recovery transaction with
locked subject, owner, session, workspace and in-flight-write checks. A changed
session, claim, fence or human hold refuses release; preservation already
completed remains on origin. Released nonterminal tasks stay `BLOCKED` with
the same retained-handoff marker, so a
dead ordinal cannot re-enter the frontier alongside its successor. Completed
and failed tasks retain their terminal status.

Unpublished commits and dirty work use the existing
`aq/preserved/<owner-row-id>` ref; dirty workspaces remain disabled. An optional
`preserve_ref` must name that same recovery ref. Remote failure returns
`unknown` and keeps the lease/local work. A crash after recovery commits but
before subject bookkeeping is replayed from the exact fence-bearing recovery
audit, without another provider probe or preservation push.

A filed task that never acquired a lease can be proved stopped only while it
is still blocked by its filing marker, with no session history, workspace holder or task-owned branch
reservation. That proof changes no domain/foreign owner's reservation.

## Wiring and operator handoff

`amber-meadow-15.4` owns CommandHandler/reconciler wiring. Supply the existing
daemon `OwnerRecovery`, its real session-provider confirmer, and the normal
routing callback. Bind only for reconciler-owned subjects, reread the subject
after a primitive updates its version, and use expiry-aware ownership in the
new publish path. Project inactivity, subject gates and manual task pauses
remain binding. Legacy/reconciler engine exclusivity and cutover evidence are
owned by the rollout tasks; this change authorizes no production cutover.

Rollback leaves existing subjects, fences, preservation refs and journals in
place and routes the feature-off engine through its existing adapters. Do not
run migrations or restart the daemon from a worker slot.

Verification is `aq test tests/test_integration_writers.py`, followed by the
integration-ownership area and the subject/selection-catalogue checks. These
use disposable PostgreSQL and real local bare Git origins, with fake clocks
and session-provider probes; they make no GitHub or LLM calls.
