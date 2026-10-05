# Git-first legacy delivery recovery

Task: `sound-apex-24`. Extends the approved Git-first integration train design
with compatibility for completed work delivered by the legacy development publisher.

## Delivery and selection

Before discovering epic targets or freezing inputs, exclude a current completion
when its exact retained provenance source is reachable from the project's designated
repository default ref (the development ref in development mode). Provenance is a
source locator, not a delivery receipt. Missing Git evidence leaves work owed.

A historical `integration_legacy_deliveries` attestation may also exclude work when
its project, repository and target match that root and its timestamp covers the
current completion generation. No attestation transfers to a later completion.
This is a read-only compatibility exception; journals, receipts, outbox and old
runtime projections remain outside train authority.

Apply the member limit after excluding delivered work and selecting the target,
so recent delivered completions cannot starve older owed inputs. Discovery probes
run concurrently outside database connections with a five-second bound; unavailable
evidence preserves owed targets, whose visits perform their own root proof.

A completed epic with no owed children produces no epic target. An already frozen
epic batch containing root-delivered inputs is held with a visible blocker, avoiding
duplicate builds, repairs and publication until a supervisor aborts it.

## Recovery controls

`integration_abort_batch` and `integration_retire_origin` run through CommandHandler,
with typed contracts, CLI preview by default, and apply requiring a nonblank reason.
Only a local operator or live named supervisor for the owning project may invoke
them. Per-project target scope checks both the task and any supplied origin.

Abort observes the target and candidate, refuses promoted work, and rechecks both
under the same batch row lock used by publication before setting intent to aborted.
The exact aborted inputs remain withheld from that target.

Retire requires the previewed origin id and delivery proof for the current completed
task. Open batches containing it must be aborted first. The transaction locks those
batch rows and the task/origin, rechecks completion, designated repository, default
ref and Git freshness, then sets retired_at. Both controls write ordinary audit
events containing their principal and reason, never replayable ref-update events.

## Visit diagnostics

A timed-out visit retains its current stage, time budget and observed batch,
candidate and target SHAs. Integration status projects these facts even when the
visit timed out before creating a batch. A subsequent tick may retry the visit.

## Validation

Real Git/disposable PostgreSQL tests cover root-contained children in all train
modes, scoped attestations and reopened generations, holding an existing duplicate
batch, preview/apply, promoted refusal, origin retirement, supervisor command
dispatch and project isolation. Train tests pin timeout stage and the read-only
legacy boundary.
