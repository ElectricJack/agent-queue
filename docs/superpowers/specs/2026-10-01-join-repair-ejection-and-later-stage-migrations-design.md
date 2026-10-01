# Join deployed migration a00000000050 with delivered repair-ejection a00000000049

Status: implementation, task `nimble-torrent`, 2026-10-01. Follows
[later repair stage constraints](2026-10-01-later-repair-stage-constraints-design.md)
(`a00000000050`) and the repair-ejection amendment of the
[hierarchical integration trains design](2026-09-04-hierarchical-integration-trains-design.md)
(`a00000000049`).

## Situation

Two lines of history left their merge base `3c34b538c` with one new Alembic
revision each, both on `a00000000048`:

| Line | Tip | Revision | Where it runs |
|---|---|---|---|
| `origin/main` (PR 727, the exact green 19-member candidate) | `4601bf42a` | `a00000000049_repair_ejection` | nowhere yet |
| Operator runtime (the main checkout) | `1c60a2a0f` | `a00000000050_later_repair_stage_constraints` | live daemon; live DB stamped `a00000000050` |

The runtime line keeps its own fix ancestry (`ef914a5d5` … `6a1a76ae1`: green
batch delegates, train control timing, published repair handoff, repair
rollover), then `dc2ab8ee3` (migration 050 and the typed constraint causes) and
`1c60a2a0f` (the notification fix cherry-picked from `51b6350ad`). Only
`ef914a5d5` is already on main, as the patch-identical `dac719b78`.

Merging them as they stand leaves the Alembic graph with two heads. The next
integration train that carries both would then deadlock: `alembic upgrade head`
raises `MultipleHeads`, every database fixture fails, and
`tests/test_migration_single_head.py` refuses the candidate.

## Decision

1. **One merge commit, both parents.** The task branch is a true merge of
   `origin/main` (first parent) with `1c60a2a0f` (second parent). Nothing is
   cherry-picked or rebased, so every deployed runtime commit stays an ancestor
   of the result, and later merges of either line reduce to fast-forward-
   compatible history. Later work that has not been validated, such as the
   accepted-delegate lifecycle branch `76e8e262` of `crisp-harbor-59` (based on
   `6a1a76ae1`), is **not** included. That task owns
   `src/integration/accepted_repair.py` and its edits to `repair.py`,
   `service.py`, `session_commands.py` and `result_queries.py`. This merge changes
   none of those files beyond what the two parents already contain, so that
   branch can rebase onto the join with no concurrent owned edits.
2. **Merge revision `a00000000051`** with
   `down_revision = ("a00000000049", "a00000000050")`, an empty `upgrade()`, and a
   `downgrade()` that only moves the stamp back to the two parents. Before
   choosing the id, I checked every local and remote branch and every worker slot
   for in-flight revisions; none was numbered `a00000000051` or higher.
   Both parents keep their original files, ids and `down_revision`
   (`a00000000048`).
   **Never re-chain `a00000000050` onto `a00000000049`:** a database already
   stamped `a00000000050` would then treat `a00000000049` as applied and never
   backfill `source_manifest`, drop the member-result FK, or install the
   ejection guards.
3. **Upgrade paths.** Alembic applies whichever parent a database lacks before
   the merge point:

   | Database stamped | `upgrade head` runs |
   |---|---|
   | `a00000000050` (the live operator DB) | `a00000000049`, then `a00000000051` |
   | `a00000000049` | `a00000000050`, then `a00000000051` |
   | `a00000000048` | both parents, then `a00000000051` |
   | empty | baseline … `a00000000048`, both parents, `a00000000051` |

   Both parents are idempotent on a baseline built from current metadata
   (`a00000000049` guards its column with the inspector and uses `CREATE OR
   REPLACE`/`DROP … IF EXISTS`. `a00000000050` rewrites a check only when its live
   text is not already `>= 0`), and they touch disjoint objects, so the order
   cannot matter.
4. **Conflict resolutions** (all other paths merged cleanly):
   - `src/integration/candidates.py`: keep both new exception types
     (`MergeConflictError` from main, `CandidateConstraintError` from runtime)
     and both constructor additions (`regenerate_*` from main,
     `confirm_published_repair` from runtime). Both lines fixed the same retained-store
     `git init` race. Keep main's private `TemporaryDirectory` staging plus atomic
     rename, which is the version in the green candidate. Runtime's
     `mkdtemp`/`rmtree` variant is behaviourally equivalent: a private init
     directory, the first rename publishes, a losing rename is discarded and its
     staging removed. Both lines' regression tests
     (`test_concurrent_store_initialization_publishes_only_complete_repository`,
     `test_concurrent_store_initializers_never_share_a_git_init_directory`) run
     against the kept implementation.
   - `tests/test_integration_candidates.py`: the public resolve-and-replay test
     keeps both parametrisations, as explicit pairs: main's migration-repair
     shapes at stage 0 and runtime's successor stage 2 without a migration
     repair. Runtime's `_retain_predecessors_and_activate_stage` helper is kept.
   - Integration-trains design: keep runtime's control-pass/outbox paragraphs
     and green-delegate continuation, with main's "every *compatible* eligible
     root PR" wording.
   - `tests/selection_catalogue.json` and the other generated outputs are
     regenerated (`scripts/regenerate-generated.sh`), never hand-merged.

## Invariants the join must keep

- A single Alembic head, `a00000000051`; no duplicate revision ids.
- After upgrading from `a00000000050`, the effects of `a00000000049` are present:
  - `integration_candidate_revisions.source_manifest` exists and is backfilled
    from current members for pre-existing revisions;
  - `fk_integration_candidate_member_results_member` is gone;
  - the `integration_result_current_member` and
    `integration_revision_manifest_immutable` triggers are installed, so a set
    manifest cannot change;
  - `integration_eject_authorized` is the manifest-aware version.
- After upgrading from `a00000000049`, the `a00000000050` effects are present:
  both stage checks read `>= 0`, so stage 2 and later resolutions and mutations
  are accepted and negative stages are still refused.
- Rows that existed before the upgrade keep their stage values and manifests.
- Deployed runtime behaviour is unchanged: the runtime line's module and test
  files are taken as-is wherever main did not also change them.

## Deployment

The operator keeps the current checkout (`1c60a2a0f`, DB `a00000000050`) until
this merge is validated and delivered. Then the operator alone moves the
checkout to the delivered commit, runs `aq db upgrade` (applying `a00000000049`
and `a00000000051`) and restarts with `aq restart --no-dashboard`. Workers never
migrate the live database or restart the daemon.

## Verification

- Real PostgreSQL: from `a00000000050` with existing candidate revisions,
  `upgrade head` applies `a00000000049` and the merge. The manifest is
  backfilled, immutable and ejection-ready, the FK is dropped, stage 2 rows are
  accepted, and the stamp is `a00000000051`. The same check runs from
  `a00000000049` with the old two-stage checks in place, and from empty.
- `tests/test_migration_single_head.py`, the 049/050 migration tests and the
  integration candidate, repair, promotion, sealing, ejection and handoff suites
  pass on the merged tree.
- `scripts/regenerate-generated.sh --check` reports no drift.
