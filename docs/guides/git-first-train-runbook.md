# Git-first integration train: activation runbook

`integration.git_first` selects the protocol the reconciler-owned integration loop
runs. `shadow` (the default) keeps the subject runtimes authoritative and only logs
Git-first comparisons. `active` replaces the root, parent and development subject
runtimes with one train ([`src/integration/train.py`](../../src/integration/train.py)).
This page is the operator procedure for switching between them. Spec:
`projects/agent-queue/specs/2026-10-04-git-first-integration-train.md` (vault).

Only an operator changes the daemon's configuration or restarts it. A worker
session never runs these steps.

After the live canary, use the
[backlog settlement checklist](git-first-backlog-settlement.md) to capture each
recorded stall's Git, check, tree-review and task-transition evidence.

## What `active` changes

- **One loop.** The integration service constructs no subject runtime and no second
  reconciler. Every five seconds it runs `integration_train_tick` through
  `CommandHandler` as the `integration-train` service principal.
- **Targets.** Every `ACTIVE` project in `train`, `hierarchy` or `development` mode
  contributes its default branch, each parent branch its completed work routes to,
  and the target of any open batch. Each target is visited by its own task; a slow
  fetch, merge or check on one target never delays another. A visit is cut off after
  900 seconds and the next tick starts a fresh one.
- **A visit.** Fetch once, freeze the completed sources not yet contained in the
  target into a batch (`integration_batches`, `integration_batch_members`), merge
  them onto the target with the shared generated-file merge, push the candidate to
  `refs/heads/aq/batches/<sha256>`, require the exact candidate's checks, and
  fast-forward the target with an expected-old push under the target's branch lease.
- **Checks.** A development project's default branch runs its pinned development
  policy's validation commands as integration jobs on a retained snapshot of the exact
  candidate (`validation: none` publishes without jobs; `advisory` publishes on red).
  Every other target requires the hosted checks named by the frozen policy's
  `root.required_checks` (`parent.required_checks` for parent branches), from the
  trusted producer only.
- **Repairs.** A red candidate's checks are refreshed once more first; a green
  result ends the repair with no attempt counted. A red or conflicting candidate then
  gets one ordinary repair task for the batch and target
  (`OrdinaryRepairService.allocate`); its counter rises when a task is filed, never
  per visit. A merge conflict first publishes the partial merge head to the
  batch ref through the managed lease. Allocation confirms that exact remote
  start before filing. Publication failure leaves the batch waiting without
  creating a repair or consuming an attempt.
  A merge conflict files its repair with a plain-English brief naming the
  conflicting member, its source OID, the conflicting files, the members already
  merged into the starting head and the members still to merge in order; generated
  files are regenerated, never hand-merged. A repaired head is published to the target
  only once it proves every frozen member's source, so a member is never dropped by a
  partial repair.
- **No replay.** The scheduler, outbox dispatch and green continuations stop.
  Progress comes from Git, cached check evidence, review evidence and batch intent
  at visit time, so a restart or missed notification is repaired by the next visit.
  Pending `integration_outbox` rows are left undelivered and are never handed to the
  old writers while `active` is set.

## Preconditions

1. **Schema at head.** Revisions `a00000000073_integration_ref_leases`,
   `a00000000074_git_batch_inputs` and
   `a00000000075_integration_check_evidence_commit_cache` are applied:
   `aq db current`, then `aq db upgrade` if behind.
2. **Reviewed bundles current.** The installed `agent-queue-root-train` and
   `agent-queue-parent-integration` bundles match the digests the project policy pins;
   see [the train policy runbook](../config/agent-queue-train-policy.md). Shadow and
   active read the same frozen policy, so its `required_checks` are what active
   requires; `tests/test_agent_queue_train_policy.py` holds the route, bundle and
   check-set pins together.
3. **Development pin.** Every development-mode project has a
   `hierarchical_integration_policy.development.route.artifact` pin whose artifact
   loads. Without one, its default branch is checked by hosted CI instead.
4. **CI triggers.** The repository's workflows run on pushes to `aq/batches/**`. In
   this repository `.github/workflows/tests.yml` and `train-candidate.yml` do
   (`tests/test_ci_trigger_policy.py`).
5. **Quiet point.** No repair writer is mid-push. Writers keep their branch leases
   across the switch; a ref leased by a live writer refuses the train's push, and a
   later visit retries it.

## Activate

1. Set the selector in `~/.agent-queue/config.yaml`:

   ```yaml
   integration:
     git_first: active
   ```

2. Restart without killing agent sessions:

   ```bash
   aq restart --no-dashboard
   ```

3. Verify:

   ```bash
   aq integration status <project>
   aq task explain <task-id>
   ```

   `projection_kind` is `train`. `targets` lists each visited target with its last
   visit state and candidate; `batches` lists open batches with intent, members and
   repairs; `blockers` names why delivery waits. The daemon log shows no repeated
   `integration train` timeouts or errors.

## Reading blockers

| Code | Meaning | Operator action |
|---|---|---|
| `awaiting_visit` | No visit since the daemon started | Wait one tick |
| `checks_pending` | Required checks not yet green on the exact candidate | Wait; check the producer if it stays |
| `checks_red` | Red on the exact candidate | A repair task is filed; follow it |
| `repair_open` | A repair task owns the candidate | Follow the named task |
| `merge_conflict` | The batch does not merge onto its target | Follow the repair task, or inspect publication evidence if filing was withheld |
| `repair_target_unconfirmed` | The repair starting OID could not be confirmed on the batch ref | Inspect the visit's publication evidence; the train retries |
| `target_moved` | The target moved | None; the next visit rebuilds |
| `source_moved` | A member's source moved after freezing | None; the next visit refreezes |
| `held` | Explicit hold or required review missing | Release the hold or review |
| `unobserved` | Git or checks could not be read | Fix the fetch or producer; evidence names the cause |
| `batch_paused` / `batch_aborted` | Operator intent | See below |

Conflict evidence in each target and batch's `detail` names the member,
partial head, reason and files; the daemon log records the same build result.
A repair claim with `needs_attention: repair_target_unpublished` found its
remote target absent and reports an integration train defect. It does not
consume slot-reset retries or create the ref from the worker slot.

## Controls

Batch intent (`open`, `paused`, `aborted`) is the only control the train reads. An
aborted batch is never rebuilt, and its exact (task, source) inputs are withheld
from that target until the task's source changes. The `pause`, `resume` and `abort`
commands that set it arrive with the integration CLI consolidation node; until then
the control is the rollback below. Do not edit `integration_batches` by hand.

## Roll back

Set `git_first: shadow` and run `aq restart --no-dashboard`. The subject runtimes
return and outbox dispatch resumes, so pending legacy events then reach their
writers as before activation. Rolling back never undoes a fast-forward the train
already published: that work is contained in its target under either protocol.
