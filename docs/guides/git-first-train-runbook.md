# Git-first integration train: activation runbook

The Git-first train is the delivery engine on every install
([`src/integration/train.py`](../../src/integration/train.py)). The compatibility
key `integration.git_first` defaults to `active` and accepts only `active`.
Trial mode has been removed. If an old config contains `git_first: shadow`,
remove the key or set it to `active` before restarting; validation explains this
migration. The retired subject runtimes are never restored by configuration.
This page covers train setup and recovery. Spec:
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
and [releases](releases.md); activation remains a per-project operator action.

## How the train delivers

- **One loop.** The integration service constructs no subject runtime and no second
  reconciler. Readiness events wake the service, with a five-second reconciliation interval.
  It runs `integration_train_tick` through
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
  Root candidates require `root.required_checks`, overridden by the current bound
  train playbook's check set when supplied. Epic/parent checks are advisory by
  default; only an enabled, ready project playbook with
  `tables.parent_episode.required_checks` opts into an epic check gate. Stored
  parent checks alone never create that gate. The
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
- **Repairs.** The production allocator is `BatchFixerService`. A conclusive red
  result is refreshed before repair; pre-existing target failures are classified
  against the exact target baseline. An unavailable baseline does not invent
  attribution. Proven inherited failures on a sealed epic can be repaired on the
  retained candidate; other unrepairable baseline failures keep bounded check
  retries and named blockers. A red promotion target remains held.

  A red or conflicting batch starts one task-free fixer session on its retained
  candidate checkout. It creates no task or claim. The brief names exact checks,
  frozen sources, conflicting paths and remaining merges. The fixer preserves all
  frozen sources as ancestors, regenerates after the combined merge, commits
  locally and uses `aq integration batch-fix --action push`. Further distinct red
  results notify the same session, within the primary time budget and at most
  three CI rounds. Exhaustion or uncertain launch reports once to the supervisor,
  releases the lane and retains work for a finite revisit; it creates no successor
  and no automatic indefinite hold. Exact current-policy green can still deliver.

  Attributed failures may split out a strict subset and its dependents. The
  original candidate stays as a companion, and a later successor includes its
  saved fixes and reuses the same fixer. The clean fixer checkout changes binding
  only through `aq integration batch-fix --action rebind`. See the
  [fixer and split protocol](hierarchical-integration-trains.md) for budgets and
  retained-work guarantees. Existing ordinary repair tasks still use their
  ancestry-checked close protocol; the active allocator files no new ones.

  A missing exact push workflow reports `ci_not_triggered` after its persisted
  five-minute deadline. With a bound train playbook, unknown CI takes a finite
  policy wait instead of allocating a code fixer. Without that policy, the
  compatibility path can allocate repair. An executing push run remains pending.
- **Legacy isolation.** The scheduler, legacy outbox dispatch and green continuations stop.
  Progress comes from Git, cached check evidence, review evidence and batch intent
  at visit time, so a restart or missed notification is repaired by the next visit.
  Pending legacy `integration_outbox` rows are left undelivered and are never handed
  to the old writers. Promotion policy events are dispatched
  through a filtered outbox page, so activated continuous flows can author train intents.

## PR gate, immediate admission and cleanup

When an epic's required children are contained in its branch and its required
reviews and any explicit epic check gate pass, the train opens its PR; maintenance retries a failed open. Leaf roots
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

Root batching follows lane occupancy, not a timer: an idle lane dispatches at
once and a busy lane batches what becomes eligible meanwhile. The `train`
block's `cadence_seconds` and `settling_cap_seconds` are deprecated. They stay
parseable for stored policy snapshots and are ignored by dispatch;
`settling_cap_seconds` survives only as the `aq doctor` no-progress alert
threshold. Epic targets freeze immediately. Sealing now is the normal path;
`seal-now` remains for operators and preserves PR, check and publication
requirements. Sources:
[src/integration/models.py](../../src/integration/models.py) (`IntegrationTrainPolicy`) and
[train_sources.py](../../src/integration/train_sources.py) (`DatabaseBatches.open_batch`).

After Git proves promotion, cleanup is tracked separately. It retires private
candidate refs and eligible member branches, comments delivery evidence on the
PR, and protects live targets. A member head that lacks safe delivery proof is
preserved or bundled rather than deleted. Aborted batches clean their candidate
refs without treating members as delivered. Settlement attempts cleanup in the
same visit; maintenance retries incomplete cleanup after failures or restart.
Each landed remote branch's cleanup also removes its matching local heads and
origin tracking refs from the retained store and registered AQ checkouts of the
same repository. Deletion requires the exact recorded head to be an ancestor of
the fetched landing target (`git merge-base --is-ancestor`). Clean idle AQ
worktrees detach at that head before deletion and remain available for reuse.
Live sessions, workspace locks, dirty or unmanaged worktrees, changed heads,
unknown Git state, and protected flow targets prevent local deletion. An already
absent remote does not skip local cleanup. Cleanup does not gate publication.
Sources: [cleanup.py](../../src/integration/cleanup.py) and
[delivery_branches.py](../../src/integration/delivery_branches.py).

## Preconditions

1. **Schema at head.** The operator checks `aq db current` and upgrades through
   `aq db upgrade` if needed. Use the current migration head, rather than the old
   cutover's revision list. Worker slots never migrate the operator database.
2. **Current project policy.** Check the designated repository, root checks,
   CI producer and committed trust manifest. The historical parent/root bundles
   do not start Subject runtimes. To install the current train policy, copy the
   shipped `fast-smooth-train` source into project scope, compile it and activate
   its reviewed artifact. For a renamed copy, bind `train.playbook_id`. Existing
   projects with neither activation nor binding keep their pre-install behavior.
   Disabling the activation stops train admission and fenced writes while worker
   source pushes and closes continue. Re-enabling resumes retained candidates.
3. **Development pin.** A development-mode project's local validation comes from
   its `hierarchical_integration_policy.development.route.artifact` pin. Without
   a loadable development pin, its default branch uses hosted checks instead.
4. **CI triggers.** Each candidate's own tree must carry workflows that run on
   pushes to `aq/batches/**`; updating only the default branch does not update
   older epic branches. In
   this repository `.github/workflows/tests.yml` and `train-candidate.yml` do
   (`tests/test_ci_trigger_policy.py`).
5. **Quiet point.** No repair writer is mid-push. Writers keep their branch leases
   across the switch; a ref leased by a live writer refuses the train's push, and a
   later visit retries it.

## Activate

1. New installs already use the train. On an older install, remove the obsolete
   trial setting from `~/.agent-queue/config.yaml`, or set the compatibility key:

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
| `ci_not_triggered` | No push workflow run on the exact candidate after five minutes | Inspect workflow filters and the policy wait or fixer. A manual dispatch does not count |
| `checks_red` | Red on the exact candidate | Inspect the retained fixer, failure groups and budget; legacy repairs may still name a task |
| `checks_preexisting` | Every failing required check also fails on the target commit, or the target has not decided it. No verified default-branch sync is available and the candidate's suites are re-requested under backoff | Fix the target's own failure, or let the batch move; `candidate_pre_existing_failure` in `detail.re_request.blocker` means three consecutive observations were unrepairable |
| `candidate_pre_existing_failure` | The bounded re-request of an unrepairable red candidate is spent | A person owns this batch: fix the target's required checks, or abort the batch |
| `repair_open` | A repair task owns the candidate | Follow the named task |
| `merge_conflict` | The batch does not merge onto its target | Inspect the retained fixer and partial candidate, or publication evidence if allocation was withheld |
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

A `visit_timeout` whose detail lists `carried_stages` is progress, not a stall.
A retained-store fetch (`fetch_snapshot`) and a candidate assembled from frozen
inputs (`build_candidate`) keep running past their visit's deadline, so a large
epic whose stages together exceed the budget advances across visits instead of
starting over on each. The lane's next visit adopts the running stage, or its
result while that result is no older than one visit budget, and continues with
the following stage. Changed inputs restart the stage. A lane whose carried stage
is still running holds its repository slot, and train status shows the stages as
`carried_stages`. A carried stage is cancelled after three visit budgets, which
the next visit reports as `CarriedStageExpired`; it is also cancelled when the
lane retires or the train stops. Only stages that write Git objects without
progress records are carried; publication, gates and settlement stay inside the
bounded visit.

Conflict evidence in each target and batch's `detail` names the member,
partial head, reason and files; the daemon log records the same build result.
For a supervisor-filed fixer that works directly on an existing candidate, bind
its publication authority before sending it to the batch branch:
`aq task set TASK --repair-batch BATCH`. The supervisor/operator assignment grants
that held task push access to the batch's candidate, preserving its canonical
task branch. The fixer can use `aq git push` or explicitly name the candidate;
the push must fast-forward the currently published candidate. Task descriptions
and `--meta` do not grant this authority. A successful candidate push wakes the
target's train visit, even after the automatic repair budget is exhausted;
delivery still requires checks for the new exact SHA and unchanged operator intent.

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
membership or forces a target ref. Trial mode has been removed; recovery uses
these batch controls and preserves the published Git history. See
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

Seal-now selects the project's unique active default integration root (`root`
or `development` kind). Epic branches and configured promotion targets such as
staging and main are excluded. An absent or ambiguous default root still refuses
both preview and apply; promotion lanes keep their own request and approval controls.

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

### Completed source CI

`aq ci completed-status --project-id PROJECT` is a read-only supervisor/operator
audit of completed tasks' recorded source commits. `failing` lists red required
checks with their exact SHA and links; `unresolved` retains pending or unavailable
evidence. This remains useful without a live train and does not change completion
or delivery state. See [the completion CI spec](../specs/design/task-completion-ci.md).

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

A withheld source is never silent. `aq integration status`, the pending visit
and `aq task explain` name each one as `source_withheld_by_aborted_batch`
(batch, source SHA, abort reason, repair attempts and the next action), and a
dependent member reports `prerequisite_withheld`; the visit reads `blocked`, not
idle. Status also lists every aborted member's disposition (`withheld`,
`pending` after a batch-owned ejection, or `released`).

### Release a withheld source

When the unchanged source should be retried on the current target, an operator
or the owning project supervisor
releases exactly one (task, source) pair from the aborted batch. Preview is
read-only and returns the frozen fences; apply needs both exact SHAs back and
a nonblank reason:

```bash
aq integration release-withheld-source <batch-id> --task <task-id>
aq integration release-withheld-source <batch-id> --task <task-id> --apply \
  --source-sha <sha> --target-sha <sha> --reason "retry on current target"
```

Release records an operator note, never a pending conversion: the aborted batch,
its abort reason and the task's completion stay immutable history. The next
batch takes a fresh id but carries the aborted batch's repair attempt count, so
the repair budget is not reset. Apply is idempotent (`already_released`). It
refuses, naming the owner decision needed, when the source or target moved, the
source is already delivered or its identity retired, an operator hold or failed
source acceptance applies, a writer session or workspace lock is live, the
batch is still active, a repair task or operation is outstanding, or the repair
budget is exhausted under `on_exhausted: human` (`repair_exhausted`: reopen the
task with feedback or archive it).

An ordinary root or development batch blocked solely by proven pre-existing
target failures can recover automatically. The train scans pending sources for
trusted green checks covering those failures, rechecks source admission and
atomically freezes a replacement containing the original members and the fix.
The old batch and checks remain history; the replacement carries the repair
count and still needs its own green candidate checks. Visit detail reports
`reassembly` with the replacement ID and added members, and the event log records
`integration.batch_reassembled`. Pauses, holds and outstanding repairs prevent
replacement; without a green fix the existing bounded suite retries continue.

### Settle a batch the target already contains

Abort refuses a batch whose target already holds every member, and such a
batch can still sit unsettled: its reused candidate's checks may never turn
green (a run cancelled because a waived platform's job was cancelled). Settle
it instead:

```bash
aq integration settle-batch <batch-id>
aq integration settle-batch <batch-id> --apply --target-sha <previewed-sha> --reason "target already matches"
```

The control requires every member source to be a Git ancestor of the observed
target, or delivery trailers for it; a matching tree alone is refused with
`target_not_settled`. Applying it records the batch as promoted at the observed
target (never the untested candidate) and logs `integration.batch_settled` with
the principal and reason; the next target visit runs epic completion. The train
settles on its own an epic refresh whose epic already is the default head (an
ancestor with an identical tree; a resolved merge still runs its checks). A refresh
whose repair published a resolved merge into the epic stays sealed while that merge
is red; abort it, or settle it once the epic has the frozen default head as an
ancestor. A run whose only non-success job lies outside the required checks no longer
holds candidate checks unknown.

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

Trial mode has been removed. A protocol setting cannot undo a fast-forward
already proved by Git. See the [policy entry](../specs/design/promotion-flow.md).
