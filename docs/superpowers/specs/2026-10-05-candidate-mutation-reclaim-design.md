# Reclaim abandoned candidate ref mutations

Candidate ref mutations retain the authority and exact expected-old/desired SHA
of one attempted write. A logical member/publication key must not make an
abandoned reservation permanent after a repair stage, lease or branch fence
changes.

Before candidate construction, rebuild or repair handoff, observe unresolved
mutations through the repository-bound App client. An exact desired remote tip
is reconciled as applied evidence using the existing protocol. Otherwise a
reserved candidate mutation may become `superseded` only when:

- Its executor claim has expired, including the bounded transport safety margin.
- The batch, current revision, operation episode/stage and project lease are
  revalidated under the project and row locks.
- The current canonical owner is a detached collector reservation or a released
  owner. An attached/pending writer, or a reserved repair writer, prevents reclaim.
- The row's recorded authority differs from current authority, or the owner has
  been durably released. Unexpected remote movement alone does not prove an
  executor stopped while its domain authority remains current.
- The mutation still has its observed identity, nonce and expiry. An observation
  race cannot retire another executor's claim.

`root_main` mutations keep their separate promotion recovery protocol. Reclaim
does not release branch ownership, accept repair lineage, change deadlines or
publish a superseded reservation's desired SHA. Unproven remote movement still
requires ordinary ancestry/publication checks.

The old row stays immutable audit evidence. Reservation walks the logical key's
superseded attempts, deriving each successor ID from its predecessor ID and the
new complete authority/SHA identity. Replays and competing executors converge on
one successor; every subsequent write still validates live authority, claims an
executor nonce and performs an expected-old push. Existing applied evidence
retains its current adoption rules.

After three identical candidate mutation refusals for the same subject generation,
the root adapter reports `candidate_mutation_identity_blocked`, with the original
reason and observed refusal count in the action journal. The subject's schedule
reports the named blocker while keeping the pinned policy's bounded revisit.
This exposes missing stop/remote proof without adding a new human approval gate.

The schema permits `superseded` for candidate mutations while requiring a null
remote SHA. Revision `a00000000077` follows `a00000000076`; it is idempotent for
databases built from current metadata and refuses downgrade while non-root
superseded evidence exists.

Verification covers stage expiry followed by a late published tip and released
writer; live executor and attached/pending/repair owners; stale observations;
successor reservation replay/exclusion; exact remote reconciliation; repeated
refusal reporting; and PostgreSQL migration upgrade, replay and downgrade guards.
