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
  result ends the repair with no attempt counted. A red candidate is then measured
  against the target commit it was built on: a failing required check the target
  also fails is a pre-existing failure, and one the target has not decided yet is
  nobody's, so neither is repaired — the visit re-requests that candidate's own
  failing check suites under bounded backoff (`baseline_*` on the batch) and files
  no repair, naming `candidate_pre_existing_failure` for a human once three
  consecutive observations have been unrepairable. Only an observed `FAILURE`
  establishes a pre-existing failure. If any required target check is `MISSING`,
  the target has no comparable baseline and candidate failures remain repairable:
  a hand-pushed target may never have run the required workflow. Status reports
  `baseline.state: unavailable`, `reason: target_required_checks_missing` and the
  missing names in `missing_target_checks`. Candidate checks must still pass on
  the exact candidate before publication. With a comparable baseline, only the
  failures the target does not fail reach the repair, named in its brief.
  A conflicting candidate then
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
  A candidate with no authenticated push workflow run on its exact SHA gets
  five minutes from the first missing-run observation. The deadline survives
  daemon restarts. After that, `ci_not_triggered` names its workflow filters
  and files an ordinary repair. The worker brings the needed workflow changes
  from the repository's default branch into a normal candidate commit and
  publishes it under the managed lease. Only a push run on the repaired exact
  head can satisfy the required checks and attestation. An existing push run
  that is still executing remains pending.
- **No replay.** The scheduler, outbox dispatch and green continuations stop.
  Progress comes from Git, cached check evidence, review evidence and batch intent
  at visit time, so a restart or missed notification is repaired by the next visit.
  Pending `integration_outbox` rows are left undelivered and are never handed to the
  old writers while `active` is set.

## Preconditions

1. **Schema at head.** Revisions `a00000000073_integration_ref_leases`,
   `a00000000074_git_batch_inputs`,
   `a00000000075_integration_check_evidence_commit_cache` and
   `a00000000080_candidate_target_baseline` are applied:
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
4. **CI triggers.** Each candidate's own tree must carry workflows that run on
   pushes to `aq/batches/**`; updating only the default branch does not update
   older epic branches. In
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
| `ci_not_triggered` | No push workflow run on the exact candidate after five minutes | Follow the ordinary repair; its brief names workflow filters and how to bring needed changes from the default branch into the candidate. A manual dispatch does not count |
| `checks_red` | Red on the exact candidate | A repair task is filed; follow it |
| `checks_preexisting` | Every failing required check also fails on the target commit, or the target has not decided it. No repair is filed and the candidate's suites are re-requested under backoff | Fix the target's own failure, or let the batch move; `candidate_pre_existing_failure` in `detail.re_request.blocker` means three consecutive observations were unrepairable |
| `candidate_pre_existing_failure` | The bounded re-request of an unrepairable red candidate is spent | A person owns this batch: fix the target's required checks, or abort the batch |
| `repair_open` | A repair task owns the candidate | Follow the named task |
| `merge_conflict` | The batch does not merge onto its target | Follow the repair task, or inspect publication evidence if filing was withheld |
| `repair_target_unconfirmed` | The repair starting OID could not be confirmed on the batch ref | Inspect the visit's publication evidence; the train retries |
| `no_regenerator` | The repository configures no regenerate command, so a generated artifact both members changed cannot be rebuilt | Configure the project's regenerate command (default `scripts/regenerate-generated.sh`); the same batch then rebuilds. No member is parked |
| `target_moved` | The target moved | None; the next visit rebuilds |
| `source_moved` | A member's source moved after freezing | None; the next visit refreezes |
| `held` | Explicit hold or required review missing | Release the hold or review |
| `unobserved` | Git or checks could not be read | Fix the fetch or producer; evidence names the cause |
| `batch_paused` / `batch_aborted` | Operator intent | See below |
| `batch_inputs_delivered_to_project` | Frozen epic inputs already landed at the project root | Abort the duplicate batch before retiring origins |
| `visit_timeout` | Visit exceeded its time budget | Inspect `evidence.stage`, target, batch and candidate; fix the named operation |

Conflict evidence in each target and batch's `detail` names the member,
partial head, reason and files; the daemon log records the same build result.
A repair claim with `needs_attention: repair_target_unpublished` found its
remote target absent and reports an integration train defect. It does not
consume slot-reset retries or create the ref from the worker slot.

## Controls

Batch intent (`open`, `paused`, `aborted`) is the only control the train reads. An
aborted batch is never rebuilt, and its exact (task, source) inputs are withheld
from that target until the task's source changes. A local operator or live named
supervisor can preview and apply an abort:

```bash
aq integration abort-batch <batch-id>
aq integration abort-batch <batch-id> --apply --reason "inputs already delivered"
```

The control refuses a promoted candidate and rechecks the target and candidate
under the batch's publication lock. It records the principal and reason.
Applying it also sets the terminal `aborted` lifecycle, releasing the member
and ancestor seals so a member can be reopened with feedback. The abort reason
and exact source withholding remain. Target visits and terminal cleanup repair
stale seals left by older aborts. Cleanup retires the private candidate under its
ref lease and releases only the batch's detached collector reservation; a live
repair lease defers cleanup without preventing member rework.

The train excludes current completions whose exact retained source is an ancestor
of the project's default/development ref. Scoped legacy delivery attestations also
exclude the completion they cover; a reopened completion requires fresh evidence.
A provenance marker locates the source and alone does not attest delivery.

To retire a delivered task's live branch origin, preview it and use the returned
`origin_id` in the apply request. Abort any open batch containing that task first:

```bash
aq integration retire-origin <task-id>
aq integration retire-origin <task-id> --origin-id <origin-id> --apply --reason "delivered"
```

Retirement rechecks the completion, origin and delivery proof, removes the origin
from pending train inputs, and records the principal and reason. Do not edit
`integration_batches` or branch origins by hand.

## Roll back

Set `git_first: shadow` and run `aq restart --no-dashboard`. The subject runtimes
return and outbox dispatch resumes, so pending legacy events then reach their
writers as before activation. Rolling back never undoes a fast-forward the train
already published: that work is contained in its target under either protocol.
