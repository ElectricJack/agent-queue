# Shared integration Git primitives

Task `agile-harbor-62.1`, implementing `rev-agile-ridge` revision 2,
§3.4 primitives 3–7 and 20. The shared argument/outcome contract comes from
the completed prerequisite `vivid-willow.1`; its published history is retained
on this branch because that contract was not yet in the assigned source base.

This is an additive implementation. No production engine, command surface,
human gate or policy is activated or removed. Phase 3's engine adapter binds
these ports through the existing command handler after the rollout gates.

## Ports and integration handoff

`src/integration/gitops.py` provides `GitOperations.bind(PrimitivePorts)` for
materialization, merging, preservation, publication and ancestry. Callers
provide a resolver returning `RetainedRepository` with the existing retained
clone, authorized GitHub repository binding, actual default branch and trusted
regeneration command. The resolver must import the frozen source/base objects
using `GitManager.afetch_repository_oid`; it must not substitute live source
branch tips or use a worker checkout. Missing objects return `source_moved` or
`unknown`, never false ancestry proof.
As in the existing services, the calling adapter binds the configured project
Git identity with `src.git.manager.commit_identity`; shared construction and
regeneration use that context through `GitManager.resolve_commit_identity`.

`SubjectGitAuthority` reads the durable subject and branch owner. It refuses a
stale version/generation, another engine, changed writer, human hold, closed
subject, stale fence or expired grant. Its required `trusted_green` callback
comes from the existing CI/evidence producer; it validates the exact head,
generation, trusted producer and required check-set version, including any
existing project promotion holds/rejections. A policy cannot bypass green on
the repository's default branch by setting `require_green=false`.
An ordinary subject can have no repair/verifier writer. Its canonical collector
then owns the lease as the subject ID (or mapped root batch ID) with role
`collector`; another subject's otherwise valid fence grants no write authority.

`SubjectGitJournal` uses the prerequisite's append-only journal. An immutable
intent commits before external writes. Completion is a separate idempotent
entry. The repository publisher exclusion is the same PostgreSQL lock used by
`development.publisher_exclusion`, including when two subjects target different
refs in one repository. After committing an intent, a bounded push holds the
subject/branch authority rows and passes the lease deadline to the existing
authenticated transport. Engine rollback, a human hold and a fence transfer
serialize with that write. Remote I/O never happens before the intent commits.

An unowned retention/cleanup key gets a released ownership tombstone inside
the bounded mutation transaction. This makes an absent key lockable without
granting it to a writer. A concurrent first writer acquisition waits until the
mutation finishes, then allocates the next fence normally.

An ambiguous transfer is reconciled by exact remote read-back. A replay does
not push again when the requested SHA is already present, and never resurrects
an applied ref that was subsequently deleted or rewound. A different tip is
`target_moved`; an unavailable read or an unconfirmed write stays
`unknown_after_push` with its durable intent pointer. A descendant remote head
is movement, not proof of the exact requested publication.

Merging uses frozen member SHAs and recorded source bases. It journals and
pins every successful member's head in `refs/aq/subjects/…` before advancing,
retains both source and target ancestry in merge commits, and returns a partial
head plus exact paths on conflict. Advancing a live source branch does not
change the reviewed source object. The live target is checked before and after
construction. Commit dates are deterministic for replay after a crash before
the member result is recorded. Reserved AQ bookkeeping paths are refused by
the shared port using the existing Git boundary; callers retain their admission and
reviewed-file guards.

The recorded base remains immutable source provenance. For each member merge,
compute the natural merge base of the running target and the frozen source head.
Use that commit as the effective base only when it is unique, descends from the
recorded base, is proved an ancestor of both merge inputs, and lies on the
running target's first-parent chain. A source ancestor reachable only through
a member's second parent may have contributed no content to the target;
advancing to it would silently drop that source's earlier changes. Otherwise keep
the recorded base, so a source contributes only its own delta even when its
origin is outside the target's history. Probe errors fail closed. This applies
to train batches and candidate construction, including accepted repair replay.
Merge results and conflict evidence name the recorded and effective bases;
generated merge commits also retain the effective base for later Git audits.
Reserved-path checks inspect both the recorded source delta and the effective
delta being merged whenever they differ, including a restoration of bookkeeping
that the target deleted. Migration collisions use the effective base.
Reviewers retain the pinned (recorded base, reviewed head, reviewed tree) view;
review diffs use the recorded base, and effective merge bases do not rewrite
source identity or review evidence.

Alembic collisions are detected by reusing `migration_heads.declaration` on
literal source declarations; branch migration code is never executed. Duplicate
revisions and sibling heads return member conflicts. Generated overlaps use
the exact merge tree's `merge=aq-generated` attributes and the existing
`regenerated_tree` scratch-worktree mechanism. A rebuild that changes another
path is a conflict. Grafts and replacement objects are refused so retained
repository customization cannot manufacture ancestry facts.

`src/integration/cleanup.py` adds `SubjectCleanup.bind(PrimitivePorts)` beside
the existing cleanup service. Its inventory port proves publication with
existing receipts and supplies exact ref/PR heads and retention facts. Keep
the existing branch-discard backup and preserved-repair recovery inventories
(including `keen-stone-14`); this task does not create another recovery system.
The live-reference port preserves writer/reference holds. The PR port must
confirm closure of the exact repository/PR/head. Successful sources follow the
policy's delete/retain choice; failed work keeps its retention deadline; the
default and subject target branches are retained. Delete intents and bounded
tries are durable. Expected-old deletions refuse moved refs, lost responses
are read back, and absent refs reconcile even after the retry budget expires.
An unexpired retention deadline returns `pending` with `retention_due_at`, so
the owning policy can revisit it without consuming a deletion attempt.

## Operator handoff and rollback

There is no operator migration, daemon restart or activation in this task.
The prerequisite's additive schema migration belongs to the operator delivery
workflow. Keep subjects on `legacy` until the owning phase's evidence and
approval gates pass; switching a subject back to `legacy` makes these mutation
ports refuse it. Existing engines and recovery tooling remain available.

Acceptance coverage lives in `tests/test_integration_gitops.py`: real Git and
disposable PostgreSQL cover conflicts, source/base movement, ancestry,
regeneration, migration collisions, intent ordering, ambiguous push/read-back,
replay, trusted exact-head green, stale ownership/holds/rollback, repository
publisher exclusion and cleanup retention. Focused and affected-area commands
are recorded in the task comments and close evidence. No full suite is needed.

## Active train repair publication (2026-10-05)

Under `git_first: active`, a conflicting batch must publish its exact partial
merge head to its stable `aq/batches` ref before an ordinary repair is filed.
Publication uses the existing managed lease, expected-old OID, batch-intent
lock and fenced transport. An unavailable or unconfirmed publication leaves
the batch retryable without filing a task or counting an attempt. Existing
candidate progress is preserved. Allocation rechecks the exact remote start
and current authorization while holding the batch and ref locks.

A partial candidate may equal the delivery target. Its presence or inclusion
in that target is insufficient to settle the batch: every frozen member must
be proved contained. A build failure exposes its outcome, member, partial
head, reason and conflicting files in the target and batch status and daemon
log; Git conflict diagnostics accompany ordinary repair instructions.

Claim preparation distinguishes a confirmed absent repair target from a
failed remote observation. Absence is a train defect, blocks the repair with
`repair_target_unpublished`, and releases its matching managed lease and claim
without consuming the slot-reset retry ladder. Unavailable Git remains an
observation failure. Claims never create a missing target ref themselves.

Acceptance uses a real Git origin, managed publication and the ordinary worker
claim path with actual checkout preparation. A conflict after one successful
member merge retains that partial head before filing; a deleted repair ref is
reported as a train defect even when a stale tracking ref remains in the slot.
The diagnosis also survives a retained attachment from an earlier preparation.
