# Task 10c independent review — d8fdcc27..8e95bc87

Reviewer: /root/review_10c_astra. Spec compliance: Needs fixes. Task quality:
Needs fixes. Critical0, Important11, Minor1. No tests rerun or mutations.

Earlier complete frontier selection, parent delivery and exact-main promotion
algorithms were outside this scoped review; prior acceptance is not re-certified.

## Strengths

Strict subject commands (contracts/integration.py148,756); isolated expected-old
empty-refspec remote deletion (git/manager.py2934,3428); cleanup terminal nonce CAS
and cleanup-independent release (cleanup.py377,release.py213); disabled source and
operator-file preservation test (root source7,test_root_integration_playbook.py47).

## Important findings (reviewer wording)

1. **Pending CI has no eventual promotion route.**
   `scripts/rebuild-reviewed-playbook-artifacts.py:271` routes `not_green` directly
   to repair. The real CI observer returns this outcome for missing or unfinished
   checks (`src/integration/ci.py:379`, `src/integration/ci.py:791`), which is normal
   immediately after publication. The installed background handler subsequently
   observes/publishes green evidence, but does not promote or emit a continuation
   event (`src/orchestrator/core.py:1573`, `src/integration/attestation.py:192`). The
   service discards handler return values (`src/integration/service.py:173`). A
   candidate that becomes green later therefore has no route back to promotion.
   Add a durable pending-CI continuation and an exact-current-green route using
   the frozen artifact. The engine test mocks immediate green
   (`tests/test_root_integration_playbook.py:173`), so it misses this path.

2. **Conflict/red repair cannot start because its replay identity differs from
   candidate construction.** `scripts/rebuild-reviewed-playbook-artifacts.py:307`
   supplies candidate `head_sha` and event `operation_id` as `starting_sha`/
   `trigger_id`. The reviewed builder already starts that operation with
   `construction_base_sha` and `batch_id` (`src/integration/candidates.py:226`).
   `RepairService.start` rejects either mismatch (`src/integration/repair.py:183`);
   operation IDs are explicitly `repair-batch-<batch_id>`
   (`src/integration/repair.py:125`). Thus even when the SHAs happen to match, the
   trigger does not, and the route terminates with `invariant_error` before
   dispatch. Consume the already-started stage through server-derived identity
   and cover real conflict/red execution. The rebuilt repair step has the same
   problem at script line299.

3. **Disabling the activation strands newly emitted operation-bound facts.**
   The new root policy depends on frozen operation routing, but the installed
   runtime loads only enabled activations (`src/playbooks/runtime.py:88`). Frozen
   destination selection then requires the owner to be present in that enabled
   set (`src/playbooks/runtime.py:247`, `src/playbooks/runtime.py:288`). After
   disabling and refreshing the activation, a sealed operation's subsequent
   repair, promoted-release, or cleanup fact cannot be accepted. This directly
   misses the disable-after-seal requirement. Extend the existing frozen-route
   admission mechanism and test through `accept_integration_event`; direct
   `PlaybookEngine` dispatch with `StubActivations`
   (`tests/test_root_integration_playbook.py:197`) does not verify it.

4. **Release replay is not canonical once scheduling progresses.**
   `src/integration/release.py:191` recognizes replay only when there is no project
   lease. After the next train acquires a lease, replaying the old released batch
   checks the new lease against the old intent and returns `invariant_error`.
   Separately, `_replay_catchup` returns whatever request is outstanding now
   (`src/integration/release.py:420`), so the original release's catch-up identity
   changes or disappears as later sweeps run. Persist or otherwise recover the
   immutable release result independently of the current lease/schedule. Existing
   replay tests stop before the next train starts
   (`tests/test_integration_cleanup.py:379`).

5. **Cleanup claims fence database results, but not duplicate PR comments.**
   Claims expire after300seconds (`src/integration/cleanup.py:146`), while
   `_cleanup_pr` performs marker lookup followed by POST without a durable
   prewrite/unresolved-publication state or a nonce/deadline check before the
   write (`src/integration/cleanup.py:208`). An old executor paused after a
   negative marker lookup can resume after takeover and POST another comment.
   A lost POST response can similarly leave a write unresolved while a retry
   sees no marker yet. The finalization CAS cannot undo that external duplicate.
   Reuse a durable publication reservation and reconcile unresolved writes before
   allowing another POST. The takeover test replaces `_perform` entirely, so it
   tests only finalization fencing (`tests/test_integration_cleanup.py:711`).

6. **PostgreSQL can leave cleanup permanently pending after all items finish.**
   `_finalize` updates its individual item, then queries all item states without
   serializing on the batch (`src/integration/cleanup.py:370`,
   `src/integration/cleanup.py:401`). Two transactions completing the last two
   items can each observe the other item as pending, skip aggregate projection,
   and commit. PostgreSQL `immediate()` is an ordinary transaction, not a global
   write lock (`src/database/queries/transaction_queries.py:72`). With no pending
   items left, the selector will never revisit this batch
   (`src/database/queries/integration_reconciliation_queries.py:190`). Serialize
   aggregate finalization consistently or provide a durable terminal-aggregate
   reconciliation path, and add the two-transaction PostgreSQL test.

7. **The materialized cleanup set omits eligible source refs and frozen retention
   decisions.** `_items` creates source PR actions, one audit PR, and the local/
   remote integration branch only (`src/integration/cleanup.py:578`,
   `src/integration/cleanup.py:609`). It neither materializes source refs nor
   consults retention policy. The available cleanup policy contains only retry
   settings (`src/integration/models.py:121`). This is an explicit missing
   deliverable, not merely broader cleanup coverage. Add the immutable source
   identities and frozen retention decision needed to materialize eligible
   source-ref cleanup.

8. **Local-ref deletion does not protect checked-out or foreign-owned branches.**
   `_cleanup_local_ref` checks only default-branch name and SHA
   (`src/integration/cleanup.py:264`). Its Git helper then runs `update-ref -d`
   directly (`src/git/manager.py:2977`), without checking worktree occupancy or
   recorded branch ownership. Expected-old deletion does not itself protect a
   checked-out branch. Verify both protections before deletion and cover
   matching-SHA checked-out/foreign branch cases.

9. **The cleanup command can report complete for a partial page or a terminal
   conflict.** The nonempty-page path declares completion when every returned
   result completed, irrespective of remaining batch items
   (`src/commands/integration_commands.py:502`). With more than100due items, the
   first successful page can therefore return `complete`. Furthermore, an executor
   finding a terminal `conflict` or `failed` item returns `already_complete`
   (`src/integration/cleanup.py:128`, `src/integration/cleanup.py:392`), which this
   command counts as successful. Project the command outcome from all durable
   batch item states and preserve terminal failure semantics.

10. **Successful worktree removal is not crash-replayable.** After
    `aremove_worktree_exact` succeeds but before finalization commits, replay
    reads the missing worktree's HEAD and records a terminal conflict
    (`src/integration/cleanup.py:341`). There is no authenticated already-absent
    reconciliation for the recorded retained worktree. Preserve enough durable
    removal evidence to distinguish completed removal from an ownership conflict,
    and test a crash between removal and `_finalize`.

11. **The required all-or-none target constraint permits a null PR number.**
    `src/database/tables.py:2785` and migration
    `migrations/versions/18cd4540cd0d_normalized_integration_cleanup.py:117` use
    `target_pr_number > 0` without `IS NOT NULL`. For an otherwise valid source/
    audit PR target with a null number, the check evaluates to SQL NULL, which
    passes a CHECK constraint. Execution later fails at `int(None)`. Require a
    non-null positive PR number in both definitions and test incomplete target
    tuples on both dialects.

## Minor / evidence

Final gate's12warnings are attributed to existing dependencies (report122,138),
inherited noise rather than a new regression. Required real-engine acceptance
substitutes domain mocks (test_root_integration_playbook.py133); PG tests cover
migrations instead of release/cleanup races (test_migration_integration_cleanup.py95).
Outside-diff named checks: candidate/repair start identity, CI background continuation,
frozen runtime admission, PG transaction semantics, frozen cleanup policy.
