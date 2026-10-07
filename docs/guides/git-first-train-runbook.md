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

## Moving the root to a promotion-flow source

Run cutover from a local operator CLI. The supervisor can read the plan, but
cannot apply or preview the control. First deliver the reviewed workflow change
through the train: `.github/workflows/tests.yml` must explicitly cover the new
default and every flow target in `pull_request.branches`. Missing coverage refuses
cutover before any branch or binding write.

Save the inventory before changing GitHub's default or rulesets:

```bash
aq integration cutover-plan --project demo --flow promotion-flow.yaml > cutover.json
```

The plan records the configuration generation, root OID, open PRs, live subjects,
owners, legacy intents and train batches, retained undelivered completions, and
GitHub's default, rulesets and classic protection. Its aborted-member section names
every undelivered member of an aborted root batch, including source SHA, reason,
time and whether Git provenance is retained. After rebind, ordinary selection on
the new default admits retained members; their old batch remains aborted and
continues withholding them on the old target. Reopen or archive work aborted for
content reasons before applying. No ejection is required.

Follow the ordered operator steps in the saved plan. Settle or abort open root
batches and resolve other listed holders, then regenerate the saved plan before
manual GitHub changes if the root or inventory changed. Use `--allow-epics` on
both commands when epic collection should continue. If the new default is
missing, run apply once to create it; `github_default_pending` leaves AQ's
configuration unchanged. Install branch and tag rulesets by hand,
then run the printed `gh repo edit` command to change GitHub's default. Cutover
verifies the App, required attestation contexts and bypass policy before changing
AQ's rows; it never edits workflows or GitHub settings.

```bash
aq integration cutover --project demo --flow promotion-flow.yaml
aq integration cutover --project demo --flow promotion-flow.yaml \
  --apply --expected-generation 7 --plan cutover.json
```

Replace `7` with the saved generation. Apply creates a missing default at the
captured root OID using a create-only lease, or verifies an existing branch has
that exact OID. A push failure or changed preview refuses configuration changes.
Branch creation may succeed before a later GitHub verification refuses; restore
the required remote policy and retry against the same saved plan. AQ's repository
default, project default, flow and scoped data fixes commit together. Receipt
updates temporarily suspend only their update trigger under a PostgreSQL table
lock, restore it before commit and verify it afterwards; exact receipt and epic
origin IDs are audited for reversal. Ordinary receipt updates remain forbidden.
Retarget the listed open PRs by hand after apply, then resume and verify delivery
to the new root.

For rollback, preview the recorded inverse and save it. Restore the recorded
workflow branch list through a reviewed change and restore the recorded GitHub
default, rulesets and classic protection by hand. The old default must contain
the current root; fast-forward it through the operator procedure if needed.
Regenerate the reverse preview after delivering any workflow restoration, so
its root OID and generation are current.

```bash
aq integration cutover-plan --project demo --reverse > reverse.json
aq integration cutover --project demo --reverse \
  --apply --expected-generation 8 --plan reverse.json
```

Reverse refuses until remote state matches the recorded state, then restores
only the audited configuration and data rows. It does not reset branches or
rewrite unrelated receipts.
The restored process is specified by the operator-vault document
`projects/agent-queue/specs/2026-10-06-git-first-train-fidelity.md`, §2.2.
For the branch-chain release path, see [promotion flows](promotion-flow.md)
and [releases](releases.md); their pending sections describe work awaiting delivery.

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
- **Stacked sources.** Before freezing, compare each source base with the fetched
  target and other live task branches. A base containing another task's unpublished
  work requires a declared `blocks` prerequisite. Otherwise `undeclared_stack_base`
  withholds that source and its dependents while independent members proceed.
  A declared prerequisite lands first in the same batch or must already be delivered;
  `stack_prerequisite_pending` withholds stacks whose prerequisite is still unfinished.
  This prevents a delta-only merge from importing an intermediate commit's ancestry
  without its content, which would make a later target sync delete that content from
  the original branch. Bases already in the target remain usable. Existing frozen
  batches are rechecked too; abort an unsafe batch to refreeze with its prerequisite.
  The scan asks Git once per off-target base (`for-each-ref --contains`) rather than
  once per live origin, and its answer is reused while the refs, target and live
  origins stand, so a repository with hundreds of archived-but-unretired branches
  costs a visit the same single query. A daemon-written source-CI repair binding
  (`integration_source_ci`) counts as a declared prerequisite: the repair's recorded
  base is the exact source head it must merge, so the pair delivers in one batch,
  source first. A red source enters only alongside that independently admitted
  repair and retains its review authorization; the combined candidate must pass. A source or repair already delivered to a target takes no part, and
  the prerequisite named is the one that publishes the base, never a sibling that
  merely merged its branch.
- **Conflicting source PRs.** An exact open, same-repository, non-draft PR with
  the configured review authorization can enter the shared batch when GitHub
  reports it dirty and an authenticated check observation proves that its PR
  workflow never ran. Its missing PR checks are not passes; integration validity
  comes from the final combined candidate's own required checks. Genuine red
  source checks without an exact admitted source CI repair, an executing workflow,
  an unavailable observation, unknown
  mergeability, explicit holds and withheld inputs still block admission. A
  conflicting epic joins the root candidate without an automatic per-epic
  refresh or another round of author checks. See the
  [source admission and batch repair contract](../specs/design/conflicting-source-batches.md).
- **Checks.** A development project's default branch runs its pinned development
  policy's validation commands as integration jobs on a retained snapshot of the exact
  candidate (`validation: none` publishes without jobs; `advisory` publishes on red).
  Every other target requires the checks named by the frozen policy's
  `root.required_checks` (`parent.required_checks` for parent branches). The
  policy's optional `ci` block chooses which runner produces them:
  `source` is `hosted` (the default; GitHub Actions, from the trusted producer
  only), `local` (the local job runner runs `ci.commands[<check name>]` on a
  retained snapshot of the exact candidate and the train reads no GitHub CI) or
  `hybrid` (candidates run locally, with hosted candidate checks and App
  attestation where required). `ci.hosted_attestation` chooses those hybrid
  boundaries: `{"root": true, "epic": false}` is the default. A hybrid root
  requires both runners' checks and App attestation before publication; only an
  explicit `root: false` in this map opts it out. Omitted map entries retain
  their defaults. `epic: true` adds the same hosted requirements to hybrid epic
  candidates. A hybrid root PR and promotion step still read hosted checks.
  `root`, `epic` and `promotion` override
  `source` per target kind. A `local` or `hybrid` boundary must name a command for
  every one of its required checks, and a policy that does not is refused when it
  is written. Local checks keep the boundary's check names and version, so
  baselines, repair briefs and gate states read the same whichever runner
  produced them. Local evidence also binds the boundary and command plan:
  changing a command or execution bound invalidates an old green result even
  without a version bump, and root and promotion evidence cannot overwrite each
  other. A purely local candidate carries no hosted attestation. Local root PR
  validation starts only after exact-head human approval and an eligible open,
  same-repository, non-draft PR, including under automatic admission; unreviewed
  member code cannot enter the trusted local job lane. Hybrid roots likewise
  require this approval before admission to their local candidate gate. A local
  promotion step runs the checks its trust manifest selects; when `ci.commands`
  lacks one the step holds with `promotion_intent_invalid` and the error
  `ci_command_missing:<name>`. A development pin
  keeps its own validation commands; it has no `ci` block.
  `aq integration status` reports the runner for each target kind as `ci_source`
  (`origin: policy`, `development` or `default`), and `aq doctor --check
  integration.ci_source` names projects that run checks locally and any stored `ci`
  block that no longer validates (such a train reads hosted checks everywhere).
  **Rollback hazard:** a stored `ci` block is an unknown policy field to older
  daemons and makes the entire policy invalid. Before downgrading, the operator
  must save the policy and remove its `ci` block through `aq project set <id>
  integration-policy`; doctor names the affected projects. Changing live CI
  policy remains an operator decision.
- **Check evidence.** Each cached check keeps its own conclusion, even when its
  workflow fails. Baseline comparison uses those individual conclusions. A red
  repair brief names the failing checks, links their jobs (or the workflow when a
  job is unavailable), and includes failing test IDs reported in check output.
  An unavailable target baseline still produces a brief naming the candidate's
  failures, without attributing them to the batch. A failed workflow with no
  identifiable failing required check blocks publication and repair allocation
  with `candidate_failing_checks_unknown`.
- **Repairs.** A red candidate's checks are refreshed once more first; a green
  result ends the repair with no attempt counted. A red candidate is then measured
  against the target commit it was built on: a failing required check the target
  also fails is a pre-existing failure, and one the target has not decided yet is
  nobody's. For an epic with only proven pre-existing failures, the train may
  file one ordinary sync repair to merge the current default branch at a pinned
  SHA. The default head must be the exact fetched candidate of a known root batch
  or carry a trusted App attestation, and its own trusted required checks must
  freshly pass every failing name. The visit records `detail.sync_default_branch`
  with the default ref, SHA, check names, provenance and allocation outcome.
  The repair keeps every frozen member, resolves conflicts and regenerates
  generated files; its resulting candidate still owes exact-head CI and
  attestation. Its ordinary dedup key allows only one sync per batch, including
  after a restart or archival. Root targets and unproven failures do not sync.
  The train inspects the pinned merge and names conflicting files in the
  existing worker conflict brief; it never resolves real code automatically.
  A completed epic whose required children are already collected gets a
  `train-epic-sync-` batch freezing those exact sources and the current epic
  head, including children with retired origins or archived tasks. This works
  after an earlier batch was aborted, without reopening work or filing children.
  If default evidence is unavailable or red, the fix is already in the candidate,
  or the sync is spent, the visit re-requests that candidate's own
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
  merged into the starting head and the members still to merge in order. That
  one repair owns every remaining member and must preserve all exact sources as
  ancestors. During merges, take either side of a generated conflict and run
  regeneration once after all sources are combined; generated files are never
  hand-merged. Automatic generated-only construction uses the same final sweep.
  A repaired head is published to the target only once every frozen source is
  an ancestor and its own exact required checks pass. Source trailers and green
  checks on a partial repair cannot authorize publication.
  A passing repair close also requires every frozen source as an ancestor;
  missing sources return to the same worker without releasing its claim.
  Frozen source commits are inherited history for the publishing identity
  check, while new repair commits still require the worker's allowed identity.
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

## Restored PR gate, cadence and cleanup

When an epic's required children are contained in its branch and that exact head
is green, the train opens its PR; maintenance retries a failed open. Leaf roots
get their PR at worker close. Before freezing a root member, the train checks
that its PR is open on the exact source head and that the boundary's required
checks pass. `admission: authorized` requires no human review;
`admission: reviewed` additionally requires an approval on that head and no
outstanding changes requested. A GitHub outage is unknown evidence with retry,
not a failed check. PR checks are admission evidence; the train separately tests
its exact candidate before publishing. Sources:
[src/integration/train_sources.py](../../src/integration/train_sources.py),
[root_pull_requests.py](../../src/integration/root_pull_requests.py) and
[reviews.py](../../src/integration/reviews.py).

Root batching uses the policy's optional `train` block:

```yaml
train:
  cadence_seconds: 300
  settling_cap_seconds: 1800
```

These are shipped defaults. The next automatic seal is the earlier of
`latest_admission + cadence_seconds` and `first_admission + settling_cap_seconds`.
A new eligible input extends the quiet period, but never the cap. Epic targets
freeze immediately. `seal-now` bypasses only cadence, once; it preserves PR,
check and publication requirements. Sources:
[src/integration/models.py](../../src/integration/models.py) and
[train_sources.py](../../src/integration/train_sources.py) (`DatabaseBatches.open_batch`).

After Git proves promotion, cleanup is tracked separately. It retires private
candidate refs and eligible member branches, comments delivery evidence on the
PR, and protects live targets. A member head that lacks safe delivery proof is
preserved or bundled rather than deleted. Aborted batches clean their candidate
refs without treating members as delivered. Cleanup does not gate publication.
Sources: [cleanup.py](../../src/integration/cleanup.py) and
[delivery_branches.py](../../src/integration/delivery_branches.py).

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

Each target also reports `timing.elapsed_seconds` and `timing.stages_seconds`,
both during its visit and after completion. These monotonic durations include
waiting for shared work. `repository_binding` measures GitHub identity lookup,
`retained_store` local repository setup, `fetch_snapshot` the Git observation,
and `resolve_checks` / `refresh_checks` the candidate check reads. The daemon
logs the completed timing breakdown under `src.integration.train.timing`,
including failed and timed-out visits. A long `select_batch` or `settle_epic`
can include admission or epic-head check reads as well as Git containment work.

Overlapping targets of one project/repository share a fetch only if it started
after they requested their snapshots, and pin their own target OIDs from its
immutable ref map. Callers arriving during a fetch wait for a shared successor,
so a burst during one fetch needs at most two fetches. A post-delivery snapshot
therefore observes a fetch started after publication. Unexpected fetch failures
and cancellation propagate to all waiting readers without repeated GitHub calls.
Setup does not fetch again; each later visit fetches afresh. Targets in different
repositories remain independent.

Validated completion records are cached by observer store, repository,
completion identity and pinned provenance OIDs. Delivery proofs bind all current task inputs,
the source base and the exact target OID. These bounded in-memory caches avoid
revalidating the same immutable Git objects on every scheduler tick. Changed
generations, refs, repositories or bases miss the cache; failed proofs are retried.
Writers still fetch and revalidate ordinary database inputs and remote freshness
before acting. Multi-target delivery views check remote refs in one call per
repository. Advisory reads retain each target's original 30-second snapshot
window. Reduced explain and pool diagnostics consume existing proofs without
launching Git; missing proofs withhold work until a background reader observes them.

To reproduce the cost of repeated train/frontier delivery proofs over 137
completions, use the disposable local benchmark:

```bash
python scripts/benchmark-git-truth.py --members 137 --passes 3 --profile /tmp/truth.prof
python -c 'import pstats; pstats.Stats("/tmp/truth.prof").sort_stats("tottime").print_stats(20)'
```

It reports wall time, Python CPU time and Git command counts for a cold pass and
subsequent warm passes, excluding repository setup. At a five-second cadence its
CPU percentage is the measured Python CPU seconds divided by five; child Git CPU,
SQL, network and other daemon work are outside this benchmark. On the 2026-10-07
development host, warm passes fell from 1,096 Git commands and 3.6 seconds to zero
commands and 8–19 milliseconds. Measure production idle CPU and explain latency
after deploying; the local proof benchmark does not establish either service-wide
target.

## Reading blockers

| Code | Meaning | Operator action |
|---|---|---|
| `awaiting_visit` | No visit since the daemon started | Wait one tick |
| `checks_pending` | Required checks not yet green on the exact candidate | Wait; check the producer if it stays |
| `ci_not_triggered` | No push workflow run on the exact candidate after five minutes | Follow the ordinary repair; its brief names workflow filters and how to bring needed changes from the default branch into the candidate. A manual dispatch does not count |
| `checks_red` | Red on the exact candidate | A repair task is filed; follow it |
| `checks_preexisting` | Every failing required check also fails on the target commit, or the target has not decided it. No verified default-branch sync is available and the candidate's suites are re-requested under backoff | Fix the target's own failure, or let the batch move; `candidate_pre_existing_failure` in `detail.re_request.blocker` means three consecutive observations were unrepairable |
| `candidate_pre_existing_failure` | The bounded re-request of an unrepairable red candidate is spent | A person owns this batch: fix the target's required checks, or abort the batch |
| `repair_open` | A repair task owns the candidate | Follow the named task |
| `merge_conflict` | The batch does not merge onto its target | Follow the repair task, or inspect publication evidence if filing was withheld |
| `repair_target_unconfirmed` | The repair starting OID could not be confirmed on the batch ref | Inspect the visit's publication evidence; the train retries |
| `no_regenerator` | The repository configures no regenerate command, so a generated artifact both members changed cannot be rebuilt | Configure the project's regenerate command (default `scripts/regenerate-generated.sh`); the same batch then rebuilds. No member is parked |
| `target_moved` | The target moved | None; the next visit rebuilds |
| `source_moved` | A member's source moved after freezing | None; the next visit refreezes |
| `undeclared_stack_base` | A source base includes another task's unpublished work without a `blocks` edge or a source-CI repair binding | Declare the prerequisite, or reopen and rebuild the source from its delivery target; evidence names the publishing task (`prerequisite_task_id`, and every candidate in `prerequisite_task_ids`) and exact OIDs |
| `stack_prerequisite_pending` | A declared stack prerequisite is neither delivered nor available in this batch | Finish or deliver the prerequisite before its dependent |
| `stack_ancestry_unknown` | Git could not inspect this member's source base, or the branches that could publish it | Restore readable Git objects; a later visit rechecks the same sources. Only members sharing that source base are withheld |
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

## Roll back

The fidelity rollout is roll-forward only. If an unpromoted batch misbehaves,
preserve its evidence and file a fix through the configured integration owner.
An operator or authorized project supervisor can:

- **Pause** the affected batch (`aq integration pause-batch <id> --apply
  --reason "investigate gate"`) to pause publication while investigating.
- **Abort** the batch (`aq integration abort-batch <id> --apply --reason "..."`)
  to withhold its exact inputs from future visits. The aborted batch is never
  rebuilt; its member tasks return to pending with PRs and approvals intact.
- **Eject** a failing member (`aq integration eject --batch <id> --task <id>
  --apply --reason "isolate failure"`) to freeze the remainder under a new
  batch ID. The ejected member returns to pending with its PR and approval intact.

Every action records the principal and reason. No command edits frozen
membership or forces a target ref. A switch to `git_first: shadow` reactivates
legacy writers and pending outbox delivery; it cannot undo a fast-forward
already proved by Git and is not the fidelity recovery procedure. See
[Controls](#controls) and
[Recovery after fidelity cutover](#recovery-after-fidelity-cutover).

## Controls

For concise command syntax, see the [Controls quick reference](hierarchical-integration-trains.md#controls-quick-reference).
### Supervisor controls

The local operator or an authorized live project supervisor uses these controls;
a worker token is out of scope. Every command below defaults to preview.
Apply uses the same command with `--apply`. Inspect the returned batch identity,
members, blockers and target before applying, then re-read
`aq integration status <project>` for the resulting intent and new batch ID.
These are command forms, not control actions executed for this documentation.

| Command form | Preview/apply behavior |
|---|---|
| `aq integration pause-batch BATCH --reason "investigate gate"` | Preview a move to `paused`; `--apply` pauses publication. It does not undo a delivered candidate or stop a repair worker. |
| `aq integration resume-batch BATCH --reason "investigation resolved"` | Preview a move back to `open`; `--apply` lets visits resume with existing frozen members and checks. |
| `aq integration eject --batch BATCH --task TASK --reason "isolate failure"` | Preview aborting the old batch and freezing its remainder under a new ID; `--apply` requires a nonblank reason. The ejected member returns to pending with its PR and approval intact. |
| `aq integration seal-now --project PROJECT` | Preview eligible inputs; `--apply` freezes them immediately, returns an existing batch, or reports `no_ready_work`. It cannot admit a member that fails its gate. |

Eject refuses a promoted batch, changed source/candidate evidence, or removal
of an undelivered prerequisite needed by the remainder. It never edits frozen
membership in place. Keep the returned `replacement_batch_id`; the old ID
remains aborted audit history. Pause/resume preserve intent evidence and the
configured publisher's fence. Sources:
[src/cli/integration.py](../../src/cli/integration.py) and
[src/integration/train_controls.py](../../src/integration/train_controls.py).

Check the local syntax without sending a control request:

```bash
aq integration pause-batch --help
```

### Abort and retire delivered origins

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

## Recovery after fidelity cutover

The fidelity rollout is roll-forward only. Pause or abort an affected unpromoted
batch, preserve its evidence and file the fix through the configured owner.
Never force-move a target or edit a batch record to simulate rollback.

`git_first: shadow` remains an optional compatibility selector in code; it
reactivates legacy writers and pending outbox delivery. It is not the restored
train's recovery procedure. A protocol switch also cannot undo a fast-forward
already proved by Git. See the [policy entry](../specs/design/promotion-flow.md).
