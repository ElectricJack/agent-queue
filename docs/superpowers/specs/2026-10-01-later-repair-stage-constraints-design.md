# Later repair stages in candidate resolutions and ref mutations

Status: implementation, task `grand-ember-16`, 2026-10-01. Follows
[continuous delivery](2026-09-30-continuous-delivery-design.md), whose revision
`a00000000048` let a repair operation retain exhausted stages and allocate
successor stages beyond ordinal one.

## Defect

`a00000000048` relaxed `ck_integration_repair_stages_ordinal` to `ordinal >= 0`
but left two rows that copy the active stage at `IN (0, 1)`:

| Table | Column | Constraint |
|---|---|---|
| `integration_candidate_resolutions` | `stage_ordinal` | `ck_integration_candidate_resolutions_stage` |
| `integration_candidate_ref_mutations` | `operation_stage` | `ck_integration_candidate_ref_mutations_stage` |

The squashed baseline builds from `src/database/tables.py`, so fresh databases
carried the same two-stage checks. Once an operation reached stage 2, every
write that records the active stage failed:

- root promotion (`integration_promote_main`): the `root_main` mutation
  reservation for an exact green candidate;
- candidate repair (`integration_resolve_candidate_member` and acceptance): the
  resolution reservation and its `repair_resolution` / `repair_handoff`
  mutations.

Root promotion caught the `IntegrityError` and, finding no canonical intent,
reported `root promotion reservation raced without canonical state`. Nothing
had raced; PostgreSQL had refused the row with `check_violation` (`23514`).

## Decision

1. **Schema.** Both columns accept any nonnegative stage: `stage_ordinal >= 0`
   and `operation_stage >= 0`, keeping the constraint names. Negative values are
   still rejected. No other constraint, foreign key or trigger changes: a
   resolution still references its exact `(operation_id, stage_ordinal)` stage
   row, mutations keep their lease and branch fence columns, and the
   append-only/monotone guards on resolutions, mutations, root intents and
   receipts are untouched. Retained earlier-stage history is never rewritten.
2. **Migration `a00000000050`** (down `a00000000048`) drops and re-adds each
   check only when the live text is not already `>= 0`, so it is a no-op on a
   baseline built from current metadata. Downgrade refuses while any resolution
   or mutation row records a stage above 1, because that evidence cannot be
   represented by the old schema; otherwise it restores `IN (0, 1)`.
3. **Typed cause.** An `IntegrityError` while reserving the root intent is a
   race only when it is a `unique_violation` (`23505`) and the canonical intent
   is then readable. Any other violation, or a unique violation with no
   canonical row, raises `RootPromotionConstraintError` (a
   `RootPromotionInvariantError`) carrying the SQLSTATE and constraint name; the
   command reports both in its `runtime_error` text. Candidate mutation
   reservations apply the same classification and raise
   `CandidateConstraintError` (a `CandidateStaleAuthority`) naming the
   constraint. The transaction still rolls back completely: no intent, member
   binding, mutation, receipt or lifecycle change survives the refusal.

Authorization is unchanged. Promotion from a later stage still requires the
exact current revision with conclusive green CI, an `active`/`escalated`
operation whose active stage row is `awaiting_completion`, the held project lease
with claim headroom, the reserved collector branch owner, the published
candidate PR and an exact attestation bound to the operation. A missing,
not-yet-green or crossed stage, a session caller and a mismatched attestation
are refused before any durable reservation.

## Audit of remaining two-stage assumptions

Every other stage-bearing column already allows nonnegative stages:
`integration_repair_operations.active_stage`, `integration_repair_stages.ordinal`,
`integration_batches.repair_stage_ordinal` and
`integration_promotion_intents.resolution_stage_ordinal`. The PL/pgSQL guards only
require stages to be monotone. Service code compares stages for equality or
order with the operation's active stage and has no literal bound. The legacy
SQLite importer copies stage values without validating them.

## Deployment and revision ordering

The live candidate (`a15aca37…` revision 0, head `4601bf42`) already carries
`a00000000049_repair_ejection` (down `a00000000048`). This fix therefore takes
`a00000000050`, never a second `a00000000049`. The operator applies
`a00000000050` from the deployed baseline before loading this code. When `4601`
reaches main, the two branches meet at a **merge revision** whose
`down_revision` is `("a00000000049", "a00000000050")`. Do **not** re-chain
`a00000000050` onto `a00000000049`: a database already stamped `a00000000050`
would then treat `a00000000049` as applied and never run it.

## Verification

- Real PostgreSQL migration upgrade from `a00000000048` with the two-stage checks
  in place, stage 2 rows accepted afterwards, negative stages rejected, idempotent
  re-run, downgrade refused with a stage 2 row and accepted without one.
- Public `integration_promote_main` at stages 2 and 3: promoted, mutation stage,
  receipts, intent members and the main ref bind the exact candidate.
- The same path refuses a not-yet-green stage, a missing stage row, a session
  caller and a crossed attestation without a durable reservation.
- An injected `check_violation` surfaces the constraint, not a race.
- Public candidate repair reservation and acceptance at stage 2 record the
  stage on the resolution and its mutations.
