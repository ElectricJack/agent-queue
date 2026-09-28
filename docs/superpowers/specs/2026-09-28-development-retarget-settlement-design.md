# Development delivery across a default-branch retarget

Task `quick-pinnacle`, 2026-09-28. Current behaviour is documented in
[development integration](../../guides/development-integration.md#when-the-default-branch-changes);
this note records why it is shaped this way.

## Incident

`matter-engine-cpp` was retargeted `main` → `vg-vt-improvements` (drain,
`integration-repository` with the new `default_branch`, `develop`). Afterwards:

* `quick-dune`, delivered to `main` the day before, read as pending on the new
  target (its source is not in that branch). The publisher merged it, parked it
  as a `merge_conflict` and filed a repair whose job was to merge `main` into
  `vg-vt-improvements`.
* A repair filed at 05:52 for `smart-torrent.1`'s park on `main` kept running;
  its branch starts from `origin/main`, so publishing it into the new target
  would have carried `main` too.
* `smart-torrent.1` and `.25` had parked on `main` before the retarget (their
  branches were based on `vg-vt-improvements`). The sweep's parked set was not
  scoped by target, so after the retarget they stayed `source_parked` although
  they fast-forward the new target; the supervisor moved the target by hand.
* The `missing_git_provenance (completion None)` label seen for
  `smart-torrent.2` is a separate close-ordering race (the task is COMPLETED
  before its completion record is saved) and is filed on its own.

## Decisions

1. **A settlement is the only database answer delivery truth honours.** Task
   metadata `development_delivery_settlement` says a generation is *not owed* to
   one target. `DeliverySnapshot.evaluate` answers `settled` (satisfied, never
   "delivered"), so readiness, admission, publisher, settlement, archive and
   status agree without new readers. Git is asked first: a settlement only turns
   `pending` into `settled`, so contained work always reads contained. It is
   fenced to repository, target and, for ordinary work, the exact completion id;
   a repair, whose purpose is fixed at filing, is settled for every generation
   and reclaimed if a live park of its manifest on that target needs it again. A
   malformed record settles nothing.
2. **Retarget: record work already on the old target as not owed** rather than
   asking the operator per task. Only configuration rows (`develop`) move the
   target; they now record `retarget.from_ref`, and older journals are read from
   the configuration, or the publisher's own batch/park row, before the first
   configuration onto the new target. Adoption rows never count. A generation
   completed before the first configuration onto the new target whose source
   the old target contains — or that was
   settled there, so hops chain — is settled (`delivered_to_previous_target`).
   Everything else stays owed. If the old branch is gone nothing is proven and
   everything stays owed. The supervisor gets one message per settlement batch
   and the journal one `settlement` row, so nothing is decided silently.
3. **A repair is owed only to the target it was filed for**
   (`repair_for_previous_target`); its evidence now records `target_ref`, older
   ones are read through their parked row or description. Repair ids are the
   manifest digest, except that a park whose manifest's repair was filed for
   another target records a target-specific `evidence.repair_id`, which every
   chain reader follows. A park on the current target whose members all turn
   out not to be owed is cancelled instead of
   repaired, and its repairs are settled with it (`repair_sources_not_owed`).
4. **Parks are scoped to their target.** The sweep's parked set and parked
   reconciliation only consider rows on the current target; rows on another
   target are cancelled with `evidence.retired`. Their sources are merged into
   the new target afresh.
5. **Runnable control:** `aq integration settle-parked <project> <operation-id>
   --reason … [--dismiss]` (LOCAL operator or the project's live supervisor,
   under the publisher lock). Settle records the row's members and repairs as not
   owed to its target; dismiss withdraws the row so its members are merged again.
   A member that completed again after it parked is refused.

## Not done here

Pausing or obsolete-closing a stale repair that a live worker still holds: it is
settled when it completes, and the supervisor is told to retire it with
`aq task close --obsolete` once its session stops.

Archiving a task deletes its metadata, settlement included, so an archived
settled task would read pending again; nothing live can depend on an archived
task (`task_dependencies` references live tasks only), so this is left as is.
