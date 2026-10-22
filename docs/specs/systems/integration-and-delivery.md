---
tags: [system-spec, integration, delivery, train, ci, repair, promotion, releases]
---

# System spec: integration-and-delivery

**System:** `integration-and-delivery`, one of the 18 systems in the Agent Queue system
inventory (vault: `projects/agent-queue/specs/2026-10-20-agent-queue-system-inventory.md`,
review `rev-bright-rapids`, approved on 2026-10-20).
**Written:** 2026-10-20, against the `dev` branch at `22cffd1b7`, for task
`eager-vault-55.7`.
**What this page is:** a map of the system as the code stands, written for an engineer
new to Agent Queue. It describes the system and decides nothing. The "Planned work"
section is a snapshot of the open tasks and in-flight specs on the date above, and it
goes stale as they land.

Where another document already explains something in depth, this page links to it
rather than repeating it. Two documents come first:

- [docs/concepts/integration.md](../../concepts/integration.md) is the concepts page.
- [docs/concepts/factory-policy.md](../../concepts/factory-policy.md) is the normative
  policy that this system implements.

The operator's day-to-day procedure is
[docs/guides/git-first-train-runbook.md](../../guides/git-first-train-runbook.md).

---

## 1. Purpose: the problem it solves

A worker agent finishes a task by pushing a commit to its own branch and closing the
task. That close records "I did the work and ran these checks". It does **not** put the
work anywhere anyone can use it. Dozens of agents finish work in parallel, each on its
own branch and base. Somebody has to get that work onto the shared branch (`dev` for
`agent-queue`), and from there to `staging`, to `main`, and into a release the operator
deploys.

Done by hand, this is the most expensive job in the factory. You merge many branches
that each touch the generated catalogues. You run CI on the result, work out which
branch broke it, and send the fix back to someone. You never push a red commit. You
never lose a commit. Integration-and-delivery does this job **without a human merge**:

- It **assembles** completed work into a candidate commit on top of the target branch
  (a *batch*).
- It **verifies** that exact candidate with the project's required CI jobs.
- It **repairs** a red or conflicting candidate by giving a bounded fix task to an
  agent. When that budget runs out it escalates to a human.
- It **publishes** a green candidate with a fenced, compare-and-swap fast-forward of the
  target branch.
- It **cleans up** the branches, worktrees and pull requests that delivered work no
  longer needs.
- It **promotes** one tested commit down a configured branch chain (`dev → staging →
  main`), with one pull request per step. The last step produces an annotated release
  tag, which `aq update` deploys.
- It **answers "is this delivered?"** from Git, for the scheduler (a task waiting on a
  prerequisite's delivery), for archive and deletion guards, and for the operator.

The system is mechanism. It makes no LLM calls. Judgment calls such as whether to
promote, when to escalate a stuck repair, or whether to hold a batch belong to
playbooks and supervisors, which act through the same commands
([factory policy](../../concepts/factory-policy.md), "Mechanism and policy").

---

## 2. Where the code lives

| Path | What it holds |
|---|---|
| `src/integration/` | The whole engine: about 130 modules and about 81k lines. The reduced Git-first train is a small part of it; the rest is retained compatibility code from earlier designs (section 6). |
| `src/integration/train.py`, `train_sources.py`, `batches.py`, `train_controls.py` | **The live delivery engine.** `IntegrationTrain`, target and batch selection, the batch store, and the operator controls. |
| `src/integration/gitops.py`, `regeneration.py`, `lock.py`, `checks.py`, `ci.py`, `attestation.py`, `candidate_baseline.py`, `repair.py` (`OrdinaryRepairService`) | Train helpers: deterministic merging, generated-file regeneration, the branch lock, required-check evaluation, trust manifests, hosted attestation, pre-existing-failure baselines and repair allocation. |
| `src/integration/service.py` | `IntegrationService`, the daemon's integration loop. |
| `src/integration/delivery_truth.py`, `git_truth.py`, `delivery_observer.py`, `provenance.py` | Delivery truth: completion provenance refs, Git-based containment proofs, and the observer that the scheduler, archive and status read. |
| `src/integration/promotion_steps.py`, `promotion_flow*.py`, `src/commands/promote_commands.py` | Promotion flows and steps. |
| `src/integration/cleanup.py`, `branch_retirement.py`, `branch_sweep.py`, `pr_cleanup.py`, `owner_recovery.py` | Cleanup, retirement and recovery. |
| `src/integration/root_pull_requests.py`, `github_review_poll.py`, `epic_delivery.py`, `stacked_branches.py` | Root and epic PRs, the PR gate, the read-only epic delivery projection, and stacked prerequisites. |
| `src/commands/integration_commands.py` | Every `integration_*` command, about 70 of them (section 3.4). |
| `src/commands/ci_commands.py` | `ci_completed_status`, `ci_baseline_status` (the `ci-main-sentinel` playbook's observation half) and `ci_repair_adopt`. |
| `src/cli/integration.py`, `src/cli/promote.py`, `src/cli/promote_rulesets.py` | `aq integration …` and `aq promote …`. |
| `src/doctor/integration_checks.py` | 14 `integration.*` doctor checks. |
| `src/install/deploy.py`, `update.py`, `deploy_history.py` | The deploy half of releases (`aq update`). |
| `src/releases/notes/` | Release notes, one file per version. |
| `src/prompts/integration_playbooks/` | Shipped integration playbooks (`promotion-continuous`, `promotion-request`, `root-train`, …). |
| `src/integrations/` | An empty package that is unrelated to `src/integration/`. |
| `src/database/tables.py`, `src/database/queries/integration_*_queries.py`, `blocked_state.py`, `migrations/integration_guards.py` | Tables, queries and the database triggers that enforce append-only and monotone rules. |

---

## 3. How it works today

### 3.1 Two protocols, one live

To read the code, you need to know that two integration designs are in `src/integration/`:

1. **The Git-first train** is live. It is *level-triggered*: on every visit it re-reads
   Git and the batch rows and decides what to do. It keeps no journal, generation or
   replay log. If you are changing delivery behaviour today, this is the code you change.
2. **The subject reconciler** is retained compatibility code. It came from the
   `rev-agile-ridge` simplification of 2026-10-02 and keeps a durable *Subject* per
   source, parent, root or promotion, with a phase journal, receipts and a staged repair
   service (`RepairService`, `runtime_contracts.py`, `engine.py`, `records.py`,
   `child_delivery.py`, `parent_subjects.py`). The 2026-10-12 retirement deleted its
   publishers and runtimes (`docs/reports/2026-10-12-legacy-consumer-retirement.md`). Its
   tables, recovery commands and many helpers remain, and some still run in maintenance.

A daemon-wide setting chooses between them: `integration.git_first` in
`src/config.py:2647`. It is restart-required.

| `git_first` | What runs |
|---|---|
| `active` | `train_for()` (`train_sources.py:3026`) builds an `IntegrationTrain`. This is the operator's setting on this box. |
| `shadow` (**the code default**) | `train_for()` returns `None`, and `core.py` sets every subject runtime to `None`. **Nothing delivers.** The `integration.delivery_engine` doctor check and `delivery_engine.py:engine_refusal` report "no engine". |

Several documents still say that `shadow` "keeps the subject runtimes authoritative".
That stopped being true on 2026-10-12 (section 6).

Every project also has a **rollout mode** in `projects.hierarchical_integration_mode`
(`ck_projects_hierarchical_integration_mode`):

| Mode | Meaning |
|---|---|
| `development` | Completed work lands on the integration branch (`dev`). |
| `hierarchy` | Parents assemble their children's work. |
| `train` | Root batches deliver to the default branch. |
| `disabled` | Manual: no writer acts. `aq integration off` previews and applies this mode ([manual integration](../design/manual-integration.md)). |
| `observe` | Read-only. |

`protection.py:required_classification` maps `development` to a GitHub App bypass and
every other mode to attested-only branch protection.

A task-level **integration mode** is a separate setting. It is direct push or pull
request, and it controls only how a worker's own close publishes its branch.
`models.py:resolve_integration_mode_with_source` resolves it in this order:

1. parent override
2. the task
3. `projects.integration_mode`
4. `integration.default_mode` (default `pull_request`)

A repository with no PR host falls back to `direct`.

### 3.2 Key concepts

- **Close vs delivery.** A close records a pushed checkpoint and the checks actually
  run. Delivery is the separate fact that the exact commit reached the target branch. A
  close never proves delivery
  ([factory policy](../../concepts/factory-policy.md) §B).
- **Completion provenance.** When a worker closes,
  `execution.py:_complete_session_task_locked` calls
  `provenance.py:record_worker_completion`, which writes the ref
  `refs/aq/provenance/completions/<digest>/<generation>`. The train selects sources from
  these refs. Close does not enqueue anything explicitly.
- **Target.** A `TrainTarget = (project_id, repository_id, target_ref)` of kind root,
  epic, development or promotion (`train.py:128`). No trains table exists; a target
  exists because work is aimed at it.
- **Batch.** One attempt to deliver a frozen set of sources to one target, stored in
  `integration_batches` and `integration_batch_members`.
  - Ids are `train-<sha256[:32]>`. Epic maintenance batches use the prefixes
    `train-epic-sync-` and `train-epic-refresh-`.
  - **Intent** is `open`, `paused` or `aborted`. **Lifecycle** is one of `sealed`,
    `promoted`, `cleanup_pending`, `aborted`, `failed` or `empty`.
  - A Git-first batch has a non-null `target_ref`. A promotion step is also a batch row,
    with `trigger="promotion"` and the step stored in `policy_snapshot["promotion_step"]`.
- **Candidate.** The target head plus the frozen members merged in order. It is
  published at `refs/heads/aq/batches/<sha256(batch_id)>`, and its intermediate merges
  are pinned under `refs/aq/batch-objects/`. Each merge commit reads
  `Integrate <task> (<sha>)` and carries `Source-base:` and `Effective-merge-base:`
  trailers.
- **Fence and lock.** A writer may move a branch only while it holds that branch's lease
  in `integration_branch_owners`.
  - The train takes the lease through `lock.py:BranchLock` as holder
    `service:integration-train`, with a 120 s TTL.
  - Every push is an expected-old compare-and-swap.
  - A stale fence raises `StaleFence`, and a held lease raises `BranchBusy`.
  - A lease that has expired is not proof that its writer has stopped
    (`writers.py`).
  - Other fences in the codebase include the per-claim fence on tasks, the legacy
    subject `writer_fence_token`, the project lease (`project_integration_leases`) and
    candidate-ref mutations.
- **Required checks.** `checks.py` evaluates CI on the *exact* candidate SHA through
  `HostedChecks` (GitHub Actions), `LocalChecks`, `ExactChecks`, `RequestedChecks` or
  `HybridChecks`.
  - Results for an exact commit are cached in `integration_check_evidence`.
  - The verdict is `red` if a required check failed or is missing, `unknown` if one is
    unavailable or cancelled, and otherwise `pending` or `green`.
  - Non-required jobs never gate. Required jobs are judged on their own, not by the
    workflow's overall conclusion
    ([required-job settlement](../design/required-job-train-settlement.md)).
- **Pre-existing failure.** `candidate_baseline.py:CandidateBaselineService` compares
  the candidate's failures against the target's baseline. Only failures that the batch
  introduced are normally repairable by the batch. When an open batch seals its own
  epic target's subtree, proven target failures are also repairable on the retained
  candidate: the seal prevents ordinary hierarchy repair. Unknown baseline checks
  never authorize this exception. Frozen members, repair budgets and publication
  checks remain authoritative.
- **Repair.** A bounded fix task filed against a red or conflicting candidate
  (section 3.3.3).
- **Receipt.** An append-only `task_delivery_receipts` row with a disposition of `code`,
  `noop`, `ineligible`, `skipped` or `failed`. The legacy protocol wrote receipts as its
  record of a landing. **The train writes none** (section 6).
- **Delivery truth.** "Is task T delivered to ref R?" is answered from Git: the task's
  recorded commit is an ancestor of R. Only immutable, OID-bound proofs are cached.
  `delivery_truth.py` and `git_truth.py` hold the two implementations, and
  `delivery_observer.py:DeliveryObserver` (line 585) serves the answer (section 3.3.6).
- **Promotion flow and step.** An ordered list of `source → target` steps in
  `projects.promotion_flow`.
  - Each step pins one exact source commit *S* on a private
    `aq/promote/<step>/<suffix>` ref and opens one pull request.
  - It publishes by fast-forwarding the target to S, optionally followed by an
    annotated tag.
  - The guide is [docs/guides/promotion-flow.md](../../guides/promotion-flow.md).

### 3.3 Main flows

#### 3.3.1 The loop and who calls the train

`run_one_cycle` belongs to daemon-core, which owns its ordering. Integration runs
**outside** that cycle:

1. `IntegrationService` (`service.py:51`) runs its own asyncio loop every 5 s. It is
   wired in `src/orchestrator/core.py`, around lines 2228–2421, together with
   `IntegrationOutbox`, `IntegrationAttestationService`, `IntegrationCleanupService`,
   branch materialization, retirement and discard, and `TrainCommandDriver`.
2. Each `tick` runs `_reconcile` in three parts: the train, then any runtimes, then
   maintenance. Maintenance has a 300 s budget per source and 60 s per item. The tick
   then dispatches the outbox. With a train attached, the outbox delivers only
   `ACTIVE_OUTBOX_EVENT_TYPES`: the `promotion.*` and `batch.*` events.
3. Maintenance callbacks include:
   - orphaned parent operations
   - batch cleanup
   - branch discard and materialization
   - owner recovery
   - `RepairService.retire_terminal_delegates`
   - `GitHubReviewPoller`
   - `RootPullRequestReconciler`
   - delivered-PR cleanup

   Maintenance has no delivery authority.
4. `TrainCommandDriver` (`train_sources.py:3002`) calls the train through
   `CommandHandler`, honouring the rule that every state change goes through a command.
   The command is `integration_train_tick`
   (`integration_commands.py:_cmd_integration_train_tick`). It accepts only the
   `integration-train` service principal and is excluded from the HTTP API. Every 60 s it
   runs `stall_alerts.alert_stalled_batches`, and then it calls `IntegrationTrain.tick`.
5. `IntegrationTrain.tick` (`train.py:468`) visits targets with
   `REPOSITORY_CONCURRENCY = 4`, a 900 s timeout per visit, and a 60–900 s backoff when
   GitHub rate-limits.

#### 3.3.2 From completed work to a green, published candidate

Each visit to a target (`_visit_target`, then `DatabaseBatches.open_batch`, then
`_visit_batch`) does the following:

1. **Fetch once and select sources.** `train_sources.py` drops withheld sources, checks
   that stacks are valid (`stacked_branches.py`) and applies the root PR gate
   (`github_review_poll.RootPullRequestGate`).
   - A leaf root gets its PR at worker close and must pass that PR's checks and review.
     A source whose PR checks are red is admitted only together with its authorized
     repair ([conflicting-source batches](../design/conflicting-source-batches.md),
     [root PR check recovery](../design/root-pr-check-recovery.md)).
   - Stacked sources whose prerequisite is not ready are refused with
     `undeclared_stack_base` or `stack_prerequisite_pending`.
2. **Decide when to seal.** A batch seals at
   `min(latest_admission + cadence, first_admission + settling_cap)`. The defaults are
   300 s and 1800 s (`IntegrationTrainPolicy`, `models.py:294`). `seal-now` bypasses the
   wait once.
3. **Freeze and merge.** `gitops.py:merge_sources` (line 496) merges the frozen members
   in order onto the target head. Its outcomes are:
   - `merged`
   - `conflict` (which includes a colliding Alembic head, `alembic_head_collision`)
   - `source_moved` (reselect)
   - `no_regenerator` (a configuration problem; it never parks the member)
4. **Regenerate generated files.** A conflict in a path that `.gitattributes` marks
   `merge=aq-generated` is never hand-merged. The merge keeps the current side.
   `scripts/regenerate-generated.sh` then runs **once** after all members are merged
   (`regeneration.py`, 600 s timeout), and the regenerated tree becomes part of the
   candidate. A failure is reported as `RegenerationFailure` or `GeneratedMergeConflict`
   ([codegen](../../contributing/codegen.md#one-command-and-never-a-hand-merge)).
5. **Publish the candidate.** `LeasedPublish` (`train_sources.py:2128`) pushes the
   candidate ref under the branch lock with an expected-old compare-and-swap.
6. **Check.** CI runs on the candidate ref. Which refs trigger CI is described in
   [integration CI boundaries](../../guides/integration-ci-boundaries.md), which is
   partly stale (section 6). The verdict follows section 3.2.
7. **Green.** `settle_on` (`train_sources.py:1908`) fast-forwards the target to the
   candidate with a fenced compare-and-swap push. The lifecycle becomes `promoted`, and
   `cleanup_settled` (line 1942) materializes and advances the cleanup items.
   - A root target also needs the daemon's hosted **attestation** check run
     (`attestation.py`; policy `hosted_attestation` is on for roots and off for epics).
   - An epic target settles through `EpicCompletions.settle` (line 2017), which opens
     the epic's root PR.

#### 3.3.3 Red and conflicting batches

`_red` (`train.py`, about line 867) handles a red candidate:

- **Every failure pre-dates the batch.** An epic target gets one sync with the default
  branch. If that sync is unavailable or spent and this open batch seals the epic's
  subtree, it files an ordinary candidate repair for the proven inherited failures.
  Mixed candidate/target failures use the same scope exception. The operator or live
  owning supervisor can preview `aq integration repair-target BATCH_ID` and apply with
  the previewed `--candidate-sha`, `--target-sha` and a nonblank `--reason`. Apply
  reobserves checks and both refs; it reuses outstanding repairs and retains the
  existing attempt budget, intent, source and branch ownership fences. It never
  reopens the epic, adds children or advances the target. Other targets get a bounded
  recheck, which ends in the
  `candidate_pre_existing_failure` blocker. A failing check that cannot be identified
  yields `candidate_failing_checks_unknown`. A red promotion target is held.
- **The batch introduced a failure, or the merge conflicted.** `_repair` calls
  `repair.py:OrdinaryRepairService.allocate`, which:
  - files a task `repair-<sha256(batch:attempt)>` with the dedup key
    `repair:<batch>:<attempt>`, type BUGFIX, at `INTEGRATION_REPAIR_PRIORITY` (1);
  - points the task at the candidate ref, and writes a "Repair input" task context that
    names the failures;
  - takes the branch lock with role `repair`.

  `RepairPolicy` (`models.py:152`) bounds the attempts:
  - a **primary** stage: 1800 s × 3
  - a **debug** stage: 3600 s × 3
  - then **human** (`_escalate_on`), or `continue` if `on_exhausted` says so.

  When the repair task closes, the fenced publication moves the candidate and
  `resume_completed` re-seals the batch.
- **The candidate drifted.** If a source moves under a batch, the batch reselects.
  - `eject` removes a member that will not converge.
  - `release_withheld_source` returns a withheld source to selection.
  - `abort-batch`, `pause-batch` and `resume-batch` change the batch intent.

  These are all `TrainControls` (`train_controls.py:59`) behind `aq integration …`.

See [batch conflict repair starts](../design/batch-conflict-repair-starts.md) for where a
repair starts. The legacy conflict path is still on disk:
`integration_candidate_resolutions`, `reconciler_repair_close.py`, `preserved_repair.py`
and `aq integration recover-preserved-repair`. The train does not use it.

#### 3.3.4 Epics and roots

An epic gets its own branch and target.

- Its children's work batches into the epic branch.
- Epic *sync* and *refresh* batches keep the epic current with the default branch. A
  refresh counts as delivered only if the default head is an ancestor of the epic and
  the trees are identical.
- When the epic's work is green, `EpicCompletions.settle` opens one pull request from the
  epic branch to the default branch, and that PR delivers as a root.
- A completed container owes an aggregate completion; a visit to an idle epic observes
  its readiness ([epic aggregate reverification](../design/epic-aggregate-reverification.md)).

Several components support this flow:

| Component | Role |
|---|---|
| `epic_delivery.py:EpicDeliveryProjection` | A read-only view of epic delivery state, used by `task_commands` and the graph layout. |
| `root_pull_requests.py:RootPullRequestReconciler` | A backstop that keeps root PRs consistent. |
| `RootDeliveryRedrive` | Backs `aq integration redrive-root`. |
| `child_delivery.py`, `parent_subjects.py`, `hierarchy.py` | The legacy per-child parent assembly. |
| `integration_recover_completed_container` | Recovers a completed container whose checkpoint is stuck ([design](../design/completed-container-checkpoint-recovery.md)). |

#### 3.3.5 Promotion and releases

The guides are [promotion flow](../../guides/promotion-flow.md) and
[releases](../../guides/releases.md), and the design is
[design/promotion-flow.md](../design/promotion-flow.md). For `agent-queue`, the flow
(`docs/config/agent-queue-promotion-flow.yaml`) has two steps:

| Step | From → to | Trigger | Approval | Back-merge | Tag |
|---|---|---|---|---|---|
| **`staging`** | `dev` → `staging` | continuous | none | yes | — |
| **`release`** | `staging` → `main` | on request (7-day TTL) | operator | — | annotated `v{version}` from `pyproject.toml`; notes from `src/releases/notes/{version}.md` |

How a step runs:

1. It runs as a promotion batch.
2. `promotion_steps.py:PromotionVisit` (line 1426) publishes in two recoverable writes:
   fast-forward the target to S, then create the tag.
3. After settlement the train emits `promotion.delivered`.

Policy lives in the `promotion-continuous` and `promotion-request` playbooks, which ship
`enabled: false`. Each project turns them on.

- Their triggers are the five `promotion.*` outbox events.
- They call `integration_promotion_policy_input`, `promote_request`, `promote_cancel`,
  `integration_promotion_publish` and `integration_backmerge_source`.

Operators use `aq promote schema | validate | prepare | request | approve | hotfix |
cancel | repair | status | list | rulesets`.

**Deploy** is a separate half. `aq update`
(`src/install/deploy.py`, `update.py`, `system_commands.py:_cmd_update_and_restart`):

1. picks the newest annotated tag matching `deploy.tag_glob` (default `v*`) that is
   reachable from `origin/<deploy.target>`;
2. refuses a lightweight or moved tag, a tag on an unrelated history, and a rollback
   across a breaking migration;
3. records the deployment in `~/.agent-queue/deploy.json` and
   `deploy-history.json`;
4. launches the updater detached.

With `track_branch` configured, the daemon checks every 300 s and runs the update
automatically (`core.py:_check_tracked_deploy`).

The name "release" also means `src/integration/release.py:IntegrationReleaseService`.
That is the legacy "release of a terminal root train", reachable only through the
`integration_release` command. The disabled `root-train` playbook used to call it, and
an active train does not.

#### 3.3.6 Delivery truth for the rest of the system

`DeliveryObserver` answers delivery questions for other systems:

- SQL picks the identities to check.
- `delivery_truth` (or `GitTruth`, `git_truth.py:106`) proves containment against an
  isolated fetched store, outside any database transaction.
- `DeliveryView.verified_on` and `fresh` let writers re-check before they act. A missing
  proof fails closed: nothing settles, archives or deletes.
- Read surfaces (status, explain, doctor) may reuse a snapshot up to 30 s old
  (`READ_MAX_AGE`). Guarded writers never reuse one.

The consumers are:

| Consumer | What it reads |
|---|---|
| **Claims** (`claim_queries.py`, around lines 631–812) | The frontier excludes a task whose prerequisite is not delivered: `frontier_sibling_prerequisite_not_delivered`, `frontier_prerequisite_not_on_default_branch` and `frontier_epic_refresh_pending`. With no snapshot available the reason is `delivery_evidence_unavailable`, and the claim fails closed. |
| **Readiness** (`src/task_graph/readiness.py`) | Maps those codes to "Waiting on delivery" or "Checking delivery evidence", reading cached snapshots only. |
| **Archive and delete** (`archive_queries.py`) | Refuse without an observer. |
| **`delivery-of` edges** (`blocked_state.py:delivery_landed`) | **Receipts, not Git** (section 6). |

#### 3.3.7 Cleanup, retirement and recovery

| Component | What it does |
|---|---|
| **Batch cleanup** (`cleanup.py:IntegrationCleanupService`) | Removes the remote refs, local refs, worktrees, source PRs and audit PRs that a settled batch no longer needs. Each removal is a restartable `integration_cleanup_items` row with attempts, a nonce and a terminal time. The train calls it directly; exceptions are logged, and maintenance re-materializes the items. |
| **Branch retirement** (`branch_retirement.py`) | Deletes only explicitly retired refs, after taking a backup bundle and writing `branch_deletion_audit`. Protected refs are `main`, `dev`, `staging`, the default branch, `gh-pages` and promotion targets ([branch retirement](../design/branch-retirement.md)). |
| **Daily checkout sweep** (`branch_sweep.sweep_checkout`, guarded by `SweepSafety`) | Runs from `core.py`. |
| **PR cleanup** (`pr_cleanup.py`) | Closes a settled task's PR without claiming that its content landed. `close-delivered-pr` closes a PR only once Git proves its work is on the default branch. |
| **Owner recovery** (`owner_recovery.py`, `integration_owner_recoveries`) | Proves that a branch owner's writer is gone and its branch is safe, preserves the branch to `aq/preserved/<id>`, and then releases the owner with a fence bump. |
| **Repository recovery** | Handles a corrupt retained Git store ([integration repository recovery](../design/integration-repository-recovery.md), [retained object integrity](../design/retained-object-integrity.md)). |

### 3.4 Operator surface

- **CLI:** `aq integration …` (`src/cli/integration.py`). The commands fall into these
  groups:

  | Group | Commands |
  |---|---|
  | Inspect | `status`, `trust-manifest`, `app-verify`, `app-setup` |
  | Batches | `pause-batch`, `resume-batch`, `abort-batch`, `eject`, `seal-now`, `settle-batch`, `record-root-noop`, `record-noop`, `release-withheld-source`, `release-held-gate` |
  | Hierarchy | `refresh-epic`, `reverify-epic`, `redrive-root`, `authorize-root`, `redrive-child`, `recover-completed-container`, `reopen-collection` |
  | Owners and origins | `retire-origin`, `restore-task-origin`, `reserve-owner`, `release-owner`, `release-stale-owners` |
  | Recovery and legacy | `clear-retained-recovery`, `close-delivered-pr`, `supersede-completion`, `reevaluate-repair`, `engine-transfer`, `off`, `migrate-provenance-refs`, `cutover-plan`, `cutover` |

  `aq promote …` and `aq update …` are covered in section 3.3.5. The symptom-to-command
  index is
  [integration troubleshooting](../../guides/integration-troubleshooting.md).
- **Doctor:** 14 `integration.*` checks, among them:
  - `delivery_engine`
  - `branch_origins` and `finished_branch_owners` (both fixable)
  - `completed_undelivered`
  - `train_batches_stalled`
  - `promotion_flow`, `promotion_stranded` and `continuous_promotion_lag`
  - `ci_source`, `ci_producer` and `trust`
  - `subjects_overdue` and `subjects_held` (these two read the legacy
    `integration_subjects` table)
- **API:** The `integration_*` and `promote_*` commands have no typed `/api/{category}`
  routes and are reachable only through `POST /api/execute`. The CI commands have typed
  routes. `src/api/pull_requests.py` serves `/api/reviews/pull-requests`.

### 3.5 Data model

All tables are in `src/database/tables.py`. Approximate line numbers are given so the
reader can find them.

| Group | Tables | Used by the train? |
|---|---|---|
| Batches and candidates | `integration_batches` (3730), `integration_batch_members` (3811), `integration_check_evidence` (4474), `integration_attestation_publications` (4538), `integration_cleanup_items` (4596), `integration_outbox` (5151) | Yes |
| Branch authority | `integration_branch_owners` (3386; `UNIQUE(repository_id, ref)`; legacy `owner_id`/`fence_token`/`handoff_state` columns side by side with Git-first `holder`/`fence`/`expires_at`), `task_branch_origins` (3265), `branch_retirements` (3238), `branch_deletion_audit` (3331), `integration_owner_recoveries` (4454) | Yes |
| Source evidence | `integration_source_ci` (706), `integration_review_evidence` (3419), `integration_root_authorizations` (3465), `task_integration_checkpoints` (3170) | Partly |
| Receipts | `task_delivery_receipts` (3636; append-only, `UNIQUE domain_key`) | Read by `delivery-of`; not written by the train |
| Repair and parents (legacy) | `integration_repair_operations` (4245), `integration_repair_stages` (4359), `integration_repair_stage_evidence` (4711), `integration_parent_episodes` (4311), `integration_child_dispositions` (4330), parent verifications (4736–4878) | No (retained) |
| Legacy reconciler and publisher | `integration_subjects` (5293), `integration_subject_journal` (5477), `integration_candidate_*` (3856–4086), `integration_promotion_intents` (3499; root intents only), `integration_root_intent_members` (4155), `project_integration_schedules` (4909), `project_integration_leases` (4946), `integration_release_results` (4960), `integration_legacy_deliveries` (5203), waiver/rollout/gate-applicability tables (4980–5131) | No (retained; the drop set is empty) |
| Per project | `projects.hierarchical_integration_mode`, `hierarchical_integration_policy`, `integration_mode`, `integration_repository_id`, `promotion_flow`, `default_branch_cutover` | Yes |

The train holds one live batch per target with a `pg_advisory_xact_lock` in
`batches.py:BatchStore._target_lock`. The unique index
`uq_integration_batches_active_project` covers legacy batches only.

**Configuration:**

- `IntegrationConfig` (`src/config.py`, about lines 2577–2735; restart-required).
  `git_first`, `default_mode` and `merge_*` are covered above.
  - `reconciler_active` (default `True`) matters only when `git_first` is not `active`.
  - `reconciler_shadow` and `publisher_stall_after` have no remaining effect.
- Per-project policy is `projects.hierarchical_integration_policy`, validated by
  `models.py:HierarchicalIntegrationPolicy` (line 384). Its parts are:
  - boundaries
  - `RepairPolicy`
  - `IntegrationCIPolicy`: `hosted`, `local` or `hybrid`
  - `IntegrationTrainPolicy`: cadence
  - `IntegrationCleanupPolicy`

---

## 4. Boundaries and interactions

This table covers the inventory's other 17 systems and where the line falls with each.

| System | Interaction |
|---|---|
| **daemon-core** | Owns the schema, migrations, `run_one_cycle` and its ordering, and wires `IntegrationService` into the orchestrator. Integration's 5 s loop runs beside the cycle, not inside it. |
| **task-graph** | Owns tasks, edges and the blocked projection. Integration supplies delivery truth for `delivery-of` edges and the frontier's delivery fences, files repair tasks as ordinary BUGFIX tasks, and restricts graph edits that a train depends on. |
| **scheduling-and-claims** | Owns the frontier query and claims. It calls `DeliveryObserver` for the claim-time delivery exclusions and fails closed without a snapshot. Repair tasks are claimed like any other task. |
| **routing-and-profiles** | Routes repair tasks. The train asks for an intelligence class, and routing chooses the profile. |
| **sessions-and-harnesses** | Owns the worker session. A passing close records completion provenance, which is the train's input. `aq git push` publishes the worker's branch. |
| **projects-workspaces-git** | Owns projects, repositories, worktrees and the `GitManager`. Integration keeps its own retained Git stores under the data directory and uses the async Git API. Branch origins (`task_branch_origins`) sit on the line: workspaces create them, and integration retires them. |
| **playbooks** | Holds integration *policy*: promotion-continuous and promotion-request, ci-main-sentinel, and supervisor triage of held batches. Playbooks act through `integration_*` and `promote_*` commands, and the train consumes no playbook output during a visit. |
| **supervisors-and-policy** | The supervisor watches stalled batches, human-held repairs and promotion requests, and holds a scoped set of integration controls. The [factory policy](../../concepts/factory-policy.md) is this system's normative contract. |
| **messaging-and-human-asks** | A repair that exhausts its budget escalates to a human (`_escalate_on`). Stall alerts and promotion approvals reach the operator through messages and the digest. |
| **reviews-and-knowledge** | Root PR review evidence (`integration_review_evidence`, `RootPullRequestGate`) comes from GitHub, not from document reviews. Approved specs only start the work the train later delivers. |
| **providers** | The repair-task model is subject to provider availability and failover. Integration itself makes no model calls. |
| **configuration-and-vault** | Supplies `integration.*`, `deploy.*` and per-project policy columns. The vault holds the reviewed playbook bundles that carry promotion policy. |
| **api-and-clients** | Integration commands go out through `/api/execute` only. `integration_train_tick` is excluded. |
| **dashboard** | Shows readiness ("Waiting on delivery"), PRs (`/api/reviews/pull-requests`), deployment history and epic delivery state. It computes no delivery state of its own. |
| **test-service-and-jobs** | `LocalChecks` and `HybridChecks` can run required checks as local managed jobs, and `aq test` is what workers run before close. Hosted CI is GitHub Actions (`.github/workflows/tests.yml`). |
| **operations-and-recovery** | Owns the doctor framework, `aq update` mechanics and `aq restart`. Integration contributes 14 doctor checks and the operator recovery commands (owner recovery, preserved repair, completed-container recovery, retained-store recovery). |
| **plugins-mcp-and-tools** | MCP is another front door to the same commands. The `git` internal plugin is unrelated to delivery. |

---

## 5. Design principles and invariants

1. **Git is the truth.** Whether work is delivered is a question about commits, answered
   by reading Git on each visit. Rows are caches, locks and audit records, never the
   answer. A missing proof fails closed. An unavailable check is never a pass
   (`unknown`).
2. **Level-triggered, not edge-triggered.** Each visit recomputes what to do from the
   current state, so a missed event or a restart costs a visit, not a stuck pipeline.
   The train keeps no journal, generation or receipts to replay.
3. **One writer per branch.** Every ref move holds the branch lease and pushes with an
   expected-old compare-and-swap. An expired lease is not proof that its writer
   stopped.
4. **One live batch per target**, enforced by an advisory transaction lock.
5. **Exact commits only.** Checks prove the exact candidate SHA. A changed candidate
   invalidates earlier checks. A promotion PR is pinned at S.
6. **Generated files are regenerated, never hand-merged**, in the train, in the
   publisher's merge driver and by humans alike.
7. **Mechanism in code, policy in playbooks.** The train makes no LLM calls. Promotion,
   escalation and triage are playbooks and supervisors driving commands.
8. **Every state change goes through `CommandHandler`**, including the train's own tick
   (`integration_train_tick`, service principal only).
9. **Bounded repair with a durable escalation.** Each failure gets one incident, a
   bounded number of attempts, and then a human hold. Nothing retries forever.
10. **Append-only evidence.** Receipts, audit rows and journals are protected by
    database triggers (`migrations/integration_guards.py`). Recovery preserves
    evidence before it deletes (backup bundles, `aq/preserved/<id>`).
11. **A daemon refusal is an answer.** Nobody uses `gh pr merge` or a direct push to work
    around it ([factory policy](../../concepts/factory-policy.md)).

---

## 6. Known gaps

All of these were verified in the code on 2026-10-20 unless marked otherwise.

1. **A default config delivers nothing.** `integration.git_first` defaults to `"shadow"`.
   Under shadow, `train_for()` returns `None` and no subject runtime is built. Several
   docs still describe shadow as a working fallback that keeps the subject runtimes
   authoritative or reactivates legacy writers:
   - [concepts/integration.md](../../concepts/integration.md)
   - [git-first-train-runbook.md](../../guides/git-first-train-runbook.md)
   - the framing of [hierarchical-integration-trains.md](../../guides/hierarchical-integration-trains.md)
     and [integration-troubleshooting.md](../../guides/integration-troubleshooting.md)

   The [git-first cutover runbook](../../guides/git-first-cutover-runbook.md) and
   [design/promotion-flow.md](../design/promotion-flow.md) state the true position.
2. **`delivery-of` edges cannot be satisfied under the train.**
   - `blocked_state.py:delivery_landed` is satisfied only by a root
     `task_delivery_receipts` row with disposition `code` or `noop`.
   - `DeliveryObserver._admit_delivery_dependents` only recomputes, and its docstring
     says "nothing here unblocks without the receipt".
   - The train writes no receipts. On the live database the last root receipt was
     written on 2026-10-05.
   - No `delivery-of` edges exist there today, so the gap is latent. The first one
     authored, for example by held ingestion's post-landing tasks, would stay blocked.
     `stark-willow-95.13` (section 7) retires receipt authority.
3. **Two protocols side by side.** About 81k lines in 130 modules, against the Git-first
   spec's target of about 12k engine lines and 10 tables. Among the duplicates:
   - the legacy staged `RepairService` (`repair.py`, lines 602 onward) sits next to
     `OrdinaryRepairService`;
   - the task close path still tries `close_reconciler_delegate` and
     `complete_accepted_delegate` first;
   - `integration_branch_owners` carries both column sets;
   - parent and epic repairs are legacy-only;
   - two Git-truth implementations exist (`delivery_truth.py`, `git_truth.py`);
   - about 15 legacy tables are retained with an empty drop set.

   The `tests/integration_ownership.json` size budget is still in phase `transition`.
4. **Dead or never-wired code:**
   - `shadow.py` is imported only by tests.
   - `GENERATED_MERGE_CONFIG` has no caller.
   - `reconciler_shadow` and `publisher_stall_after` have no effect.
   - `core.py:_dispatch_integration_repair_stage` is dead.
   - `WriterPrimitives` is used only by tests. The unused `ParentPrimitiveAdapters`
     implementation was removed during the hibernation epic delivery recovery;
     its policy fact models and historical refusal markers remain available.
   - `orchestrator.promotion_service`, `root_promotion_service`,
     `integration_release_service`, `parent_subject_runtime` and
     `development_subject_runtime` are always `None`, yet `IntegrationService` still
     accepts the subject-runtime arguments.
   - `CancelledCollectionRecovery` receives `parent_source.promotion`, which may be
     `None` (not traced to the end).
   - `IntegrationReleaseService` is reachable only by the manual `integration_release`
     command.
   - `ENDED_LIFECYCLES` lists `"failed"`, which nothing writes.
5. **Operator-only recovery modules accumulate.** Each answers one historical incident:
   `preserved_repair`, `parent_repair_heads`, `repair_rebind`, `detached_repair_rebind`,
   `noop_repair`, `published_repair_handoff`, `source_repairs` and `delegate_release`.
6. **Tail latency.** The 2026-10-19 retrospective (vault; review `rev-quick-pinnacle`,
   withdrawn) measured pass-to-`dev` over 22 September to 20 October:

   | Measure | Value |
   |---|---|
   | Median | 0.46 h |
   | p90 | 6.3 h |
   | Share of waiting hours spent on the 6% of tasks slower than 8 h | 72% |
   | Conflict repairs that touched a generated file | 52% |
   | Stale state rows (reservations, working checkpoints, reserved owners) | 1,870 |
   | Repair tasks that were repairs of repairs | 40 of 208 |

   Section 7 is mostly the answer to these numbers.
7. **No stall visibility beyond `train_batches_stalled`.** There is no watchdog, no
   latency telemetry, and no "why is this waiting" view across stages (stark-willow-95.3
   to .5).
8. **The timed window and task-per-repair remain.** The 300 s cadence and 1800 s
   settling cap are still the defaults, and every repair is a new task with its own
   claim and worktree.
9. **Promotion beyond `staging` has not run.**

   | Ref | Tip |
   |---|---|
   | `origin/main` | `ad6a8e9ed`, 2026-10-07 |
   | `staging` | about 870 commits ahead of `main` |

   There is no `v*` tag on origin. The release step is configured but no release has
   been cut. Step intent `promote-51310235aa3e3abe39f05fef` ("Promote staging
   22cffd1b7236") is in progress.
10. **Stale or wrong documentation:**
    - [reference/modules/integration.md](../../reference/modules/integration.md) lists
      29 modules, ten of which no longer exist (for example `subjects.py`,
      `reconciler.py`, `candidates.py`), and omits `train.py`, `train_sources.py`,
      `batches.py` and `delivery_observer.py`.
    - [integration-ci-boundaries.md](../../guides/integration-ci-boundaries.md) omits
      the PRs into `dev` and `staging` and the `aq/promote/**` and `aq/backmerge/**`
      pushes that `tests.yml` covers.
    - `PromotionPublisher`, named in the promotion-flow and releases guides, is
      `PromotionVisit` in code.
    - Historical guides point to the mixed
      [hierarchical-integration-trains.md](../../guides/hierarchical-integration-trains.md)
      as "current" rather than to the train runbook.
    - The comments in `.gitattributes` and `regenerate-generated.sh` credit the retired
      development publisher.
    - `hierarchical-integration-trains.md` says the legacy engine was removed on
      2026-10-04, while its code remains.
    - The CLI help still says "Subjects", and the command catalogue gives many
      `integration_*` and `promote_*` commands the same placeholder description.
11. **Inconsistencies in the schema and policy:**
    - `projects.hierarchical_integration_policy` and several
      `integration_promotion_intents` columns are `JSON`, not `JSONB`. It is unverified
      whether they are allow-listed in `tests/test_migration_json_columns.py`.
    - `src/policy_profiles/models.py`'s mode literal omits `development`, which the
      table check constraint allows.
    - `SubjectEngine` has only `RECONCILER`, while the column defaults to `legacy`.
    - `INTEGRATION_REPAIR_PRIORITY = 1` is commented as a stopgap.
12. **Review-state drift:**
    - [design/manual-integration.md](../design/manual-integration.md) cites
      `rev-fair-lantern` revision 2 as its authority. The review database records
      `rev-fair-lantern` as **approved at revision 2**, but the vault spec file describes
      a revision 3 "in review". Revisions 3 and 4 exist only as vault decision notes.
    - The system inventory review `rev-bright-rapids` was approved on 2026-10-20, but the
      sibling `task-graph` system spec (`eager-vault-55.2`) quotes it as in review.

---

## 7. Planned work

This snapshot was taken on 2026-10-20 by a read-only query of the open `agent-queue`
tasks, the `doc_reviews` table and the vault. Vault specs are not in this repository;
their review ids and states are quoted as they stood that day.

### 7.1 Open tasks that change this system

Almost all of it is the **fast smooth integration** epic, `stark-willow-95`
(IN_PROGRESS). Its work so far lives on
`origin/aq/epic/fast-smooth-integration-implementation`, which is **not yet on `dev`**.

The following children are complete on the epic branch:

| Child | What it delivered |
|---|---|
| `.1`, `.2` | A strict Git containment contract (`docs/specs/design/strict-git-containment.md` on the epic), retained source identity, and a guarded containment cutover (`aq integration cutover-containment`) that records legacy dispositions. |
| `.6` | Event-driven epic and root-leaf readiness without receipts. |
| `.19` | A Git-truth reconciler (`truth_reconciler.py`, `integration_reconcile_git_truth`) that derives owners, checkpoints, branch origins and delivery from Git every cycle and heals rather than refuses. |
| `.20` | Regeneration at batch assembly. The `dev` code already regenerates once per assembly. |
| `.21`, `.22` | Repair and merge helpers. |

The open children:

| Task | State | What it changes |
|---|---|---|
| `stark-willow-95.3`: redrive stuck delivery once | READY | A new `delivery_watchdog.py`. The threshold is `max(1800 s, 2 × p90 batch CI)` per project. It tells slow from stuck using wait records, makes one fenced redrive, puts policy in a reviewed V2 playbook, and adds doctor stall checks and one waiting view. |
| `stark-willow-95.4`: completion-to-dev metrics | READY | Generation-based latency with a per-stage breakdown (`src/metrics/delivery_latency.py`, the metrics API). Depends on `bold-zenith-11.1`. |
| `stark-willow-95.5`: dashboard delivery panel | DEFINED | Median, p90, breakdown, backlog and shared blockers. Depends on `bold-zenith-11.8`. |
| `stark-willow-95.7`: idle/busy dispatch | READY | Replaces the timed cadence and settling cap with lanes: depart immediately when idle, queue while busy, depart again on settle. The cadence fields stay parseable but are ignored. Needs hibernation P1/P2. |
| `stark-willow-95.8`: deterministic failure groups | DEFINED | Pure functions that group a red batch's failures, with build failures taking precedence. |
| `stark-willow-95.9`: parallel fixers | DEFINED | One fixer per failure group on `aq/batch/<id>/fix/<group>` (`max_parallel_fixers` 4, `max_rounds` 3), merge-back and one CI run, and a batch split with ejection records. The rev-4 decision notes rewrite it as in-place repair on the batch branch, with no new task, claim or worktree and no repairs of repairs. |
| `stark-willow-95.10`: advisory epic checks | DEFINED | Epic-level checks stop gating unless a project opts in. The task says `parent.required_checks.mode`; the later decision notes put the switch in playbook data. |
| `stark-willow-95.11`: warm workspaces | DEFINED | Candidate and fixer workspaces kept warm, with a shared compiler cache (`workspaces.build_cache`). |
| `stark-willow-95.12`: native-storage relocation | DEFINED | Tooling and a Matter Engine runbook (moving off `/mnt/d`). |
| `stark-willow-95.13`: one admission view | READY | One `AdmissionView` for the frontier, pools, explain and the delivery guards. It retires receipt authority and drops the SQL receipt fallback, which closes gap 2. Depends on `bold-zenith-11`. |
| `stark-willow-95.14`: stacked prerequisites by default | DEFINED | Removes the `agent-queue` special case in `stacked_branches.py`. |
| `stark-willow-95.15`: author-assisted parent merge | DEFINED | A new `close_merge.py`, `integration_continue_close` and an `integration_close_attempts` table, with author budgets as verified waits. |
| `stark-willow-95.16`: speculative next-batch CI | DEFINED | Opt-in, with `train.speculative_ci` defaulting to false. |
| `stark-willow-95.17`: align policy and ratchets | DEFINED | Aligns profiles, instructions and docs. Adds a no-active-receipt-authority ratchet and deprecates `cadence_seconds` and `settling_cap_seconds`. |
| `stark-willow-95.18`: validate and cut over | DEFINED | Validates the complete flow on disposable repositories and gives operators cutover manifests for `agent-queue` and Matter Engine. |

Other open tasks that touch this system:

| Task | State | What it changes |
|---|---|---|
| `promote-51310235aa3e3abe39f05fef`: "Promote staging 22cffd1b7236" | IN_PROGRESS | A daemon-authored promotion step intent: the live `staging` step. |
| `clear-impact-72.1`, `.7`, `.8` | READY | Evaluate rewriting the `ci-main-sentinel`, `promotion-continuous` and `promotion-request` playbooks onto the new playbook building blocks. They wait on the playbooks refactor. |
| `bold-zenith-11`: event-driven hibernation and verified waits | IN_PROGRESS (`.22` READY) | Supplies the wait records that `stark-willow-95.4`, `.5`, `.7` and `.13` build on. |
| `calm-grove-14`, `vivid-rapids-36` | READY | Remove duplicate human publishers and reconcile repository policy, then verify live acceptance. They touch repository policy and branch protection alongside integration. |
| `eager-vault-55`: system map part 2 | IN_PROGRESS | This page and its 17 siblings. |

**Deletion:** stage 4 of the Git-first train spec, and phase 4 of the `rev-agile-ridge`
plan (`quick-glacier-88`), would remove the retained subject reconciler and bring the
engine down to its line and table caps. **No open task carries this work today.**

### 7.2 In-flight and governing specs

| Spec | State on 2026-10-20 | What it changes |
|---|---|---|
| Fast smooth integration (vault, `specs/2026-10-12-fast-smooth-integration.md`), with decision notes `2026-10-13-…-rev2-decisions.md`, `2026-10-13-…-rev3-playbook-policy.md` and `2026-10-20-…-rev4-retrospective-decisions.md` | `rev-fair-lantern`: approved at revision 2 in the review database; the file describes revision 3 as in review; revision 4 exists as decision notes only | The source of `stark-willow-95`. Five stages: (1) truth, cutover, watchdog, telemetry; (2) policy, dispatch, failure groups, fixers; (3) advisory epic CI, caches, relocation; (4) admission, stacking, close merge, docs; (5) speculative CI. Revision 3 moves the on/off switch and required checks into a per-project `fast-smooth-train` playbook. Revision 4 adds in-place batch repair, drift fixed inside the integration branch, the per-cycle Git-truth reconciler, regeneration at assembly, retention of exact source objects, and latency measured from approved spec to `dev`. |
| Retrospective: why work stalls before reaching `dev` (vault, `specs/2026-10-19-retrospective-…`) | `rev-quick-pinnacle`, withdrawn ("we already created work based off this review") | The evidence base for revision 4 (gap 6). |
| Git-first integration train (vault, `specs/2026-10-04-git-first-integration-train.md`) | `rev-fair-impact`, approved | The design of the live engine. Stages 1–3 are landed. Stage 4 (deleting the reconciler; 12k engine lines, 20k total, 10 tables) is open. |
| Git-first train fidelity and dev/main/stage building blocks (vault, `specs/2026-10-06-git-first-train-fidelity…`) | `rev-vivid-quest`, approved | Restored the epic PR, the PR gate, root cadence, per-batch cleanup, `RepairPolicy` escalation, the batch controls and promotion building blocks. Implemented. |
| Dev-branch releases, revision 3 (vault, `specs/2026-10-05-dev-branch-releases-revision-3.md`) and its plan | `rev-smart-lantern`, approved; plan `rev-agile-apex`, approved | Composable promotion steps, `aq promote`, the promotion playbooks and tag deploys. Largely implemented; no release cut yet. |
| Train mechanism inventory and simplification (vault, `specs/2026-10-02-integration-train-mechanism-inventory-…`) | `rev-agile-ridge`, approved at revision 2 | The subject reconciler. Phases 0–3 landed and were then reduced by Git-first. Phase 4 is open. |
| App-mode integration train, GitHub App credentials (vault, `specs/2026-09-27-app-mode-integration-train-…`) | `rev-swift-glacier`, approved at revision 2 | Exact-SHA Actions trust, the daemon attestation check run, repository trust anchors, `app-setup` and `app-verify`. Implemented. |
| Epic pull requests and the integration train (vault, `specs/2026-09-23-epic-pull-requests-…-2.md`) | `rev-stark-summit`, approved; plan `rev-sharp-quest`, approved | One PR per epic and the removal of the development publisher. Implemented, with a few `DevelopmentPublisher` references left. |
| Retained Git corruption (vault, `specs/2026-10-14-retained-git-corruption-…`) | `rev-swift-flare`, approved at revision 2 | Retained-store detection and self-heal. Implemented ([design](../design/integration-repository-recovery.md)). |
| Canonical origin admission, expired reservation lease (vault, `specs/2026-10-13-canonical-origin-admission-…`) | `rev-prime-current`, approved | A fenced re-lease path for origin admission. Implementation not verified. |
| Event-driven hibernation and verified waits (vault) | design `rev-bold-pinnacle`, approved at revision 2; implementation `rev-fleet-orbit`, approved | The wait vocabulary that fast smooth integration builds on. |
| Dashboard feedback and versioned releases (vault) | `rev-bright-ember`, approved; plan `rev-amber-delta`, in review | The deployment history and release surfaces in the dashboard. |

---

## 8. Further reading

Current behaviour:

- [Integration](../../concepts/integration.md): the concepts page (mind gap 1).
- [Factory policy](../../concepts/factory-policy.md): the normative admit, deliver and
  recover contract.
- [Git-first train runbook](../../guides/git-first-train-runbook.md): the operator's
  procedure.
- [Integration troubleshooting](../../guides/integration-troubleshooting.md):
  symptom-to-command.
- [Promotion flow](../../guides/promotion-flow.md) and [releases](../../guides/releases.md).
- [Hierarchical integration trains](../../guides/hierarchical-integration-trains.md):
  train controls, mixed with subject-era material.
- [Integration CI boundaries](../../guides/integration-ci-boundaries.md) (partly stale).
- [Codegen: one command and never a hand-merge](../../contributing/codegen.md#one-command-and-never-a-hand-merge).
- Design notes that are current:
  - [promotion flow](../design/promotion-flow.md)
  - [manual integration](../design/manual-integration.md)
  - [conflicting-source batches](../design/conflicting-source-batches.md)
  - [batch conflict repair starts](../design/batch-conflict-repair-starts.md)
  - [required-job train settlement](../design/required-job-train-settlement.md)
  - [root PR check recovery](../design/root-pr-check-recovery.md)
  - [epic aggregate reverification](../design/epic-aggregate-reverification.md)
  - [completed container checkpoint recovery](../design/completed-container-checkpoint-recovery.md)
  - [integration repository recovery](../design/integration-repository-recovery.md)
  - [retained object integrity](../design/retained-object-integrity.md)
  - [branch retirement](../design/branch-retirement.md)
  - [integration identity diagnostics](../design/integration-identity-diagnostics.md)
- Module reference: [docs/reference/modules/integration.md](../../reference/modules/integration.md)
  (stale; gap 10).

Historical records (the code wins where they disagree):

- [Development integration](../design/development-integration.md) and its
  [guide](../../guides/development-integration.md): the retired development publisher.
- [Development conflict repair](../design/development-conflict-repair.md): its repair
  principles carry into the train.
- [Integration migration](../../guides/integration-migration.md),
  [reconciler shadow cutover](../../guides/reconciler-shadow-cutover-runbook.md),
  [git-first cutover](../../guides/git-first-cutover-runbook.md) and
  [git-first backlog settlement](../../guides/git-first-backlog-settlement.md).
- [Hierarchical integration trains design](../../superpowers/specs/2026-09-04-hierarchical-integration-trains-design.md)
  and [its review](../../superpowers/specs/2026-09-04-hierarchical-integration-trains-review.md).
- [CI main sentinel](../../superpowers/specs/2026-09-05-ci-main-sentinel-design.md).
- About 50 dated point-fix specs in `docs/superpowers/specs/` (2026-09-15 to 2026-10-13)
  record individual repair, recovery and subject-reconciler fixes. Most belong to the
  retired subject and publisher era.


## Source and recovery contract details

These implementation notes preserve the detailed contracts formerly embedded in
the modules below. Each module keeps a short contract summary beside its code.

### `src/integration/source_repairs.py`

Whether one exact train source generation is already on its default target.

Source-CI repair exists to recover red CI on a source branch whose work has
not reached the default branch.  When git already proves that generation's
work on the project's designated default branch, repairing the branch
repeats delivered work: on the box where this was found, a root whose source
was already integrated and adopted under other commits spent a whole agent
re-running the same shared fixes a second time.

:func:`prove_source_delivered` is that one answer, and it deliberately asks
the question the root scheduler already asks and trusts
(the historical train frontier on
top of :mod:`src.integration.delivery_truth`): the *exact* completion
generation, the *exact* repository and the *exact* default target ref.  A
merge, a squash and a cherry-pick are equally delivered, because the proof
names the generation's retained source and asks git whether it is contained
in the target, not whether the observed pull-request head happens to be an
ancestor.

Two rules make this safe to act on, and both are why nothing here is a cache:

* **Every decision observes afresh.**  The default target can be retargeted,
  the target can lose containment, and a delivery or an adoption can arrive
  after an earlier negative answer, so a remembered answer answers a question
  nobody asked.  Admission asks before it files, and
  :func:`delivered_queued_repairs` asks again before a claim withholds a
  queued delegate.
* **A recorded proof is audit, not authority.**
  :func:`record_delivery_evidence` writes what was observed onto the
  observation so an operator and ``aq task explain`` can see it, and nothing
  reads it back to decide eligibility.  The claim frontier has no delivery
  predicate for exactly this reason.

Everything that is not a proof answers *not delivered*, because the failure
mode that matters is stranding real work:

* an unavailable observer, a repository the project does not designate, or a
  missing target -- no canonical evidence, so repair;
* an unknown git error or a missing retained source -- unknown, so repair;
* a new checkpoint generation or a reopened task -- the evidence was gathered
  about an identity that no longer exists, so repair;
* ``no_artifact`` and ``settled`` -- neither is delivery to the default
  branch, so repair.

Only ``contained`` or proven ``no_change`` on the exact target, revalidated
against the exact source identity the observation names, may withhold work.

An unclaimed READY delegate can be retired by the command handler after a
fresh proof and a locked identity check. A claimed delegate keeps its writer;
its completed repair remains deliverable even after the original lands.

Discarding the source completion revokes the purpose of every bound delegate.
Reopen retirement fences writers and retires completed repairs in that same
transaction. Train admission and publication recheck the current source head,
including the full lineage of repairs of repairs: that recheck is
:func:`~src.integration.delivery_truth.superseded_source_repairs_on`, which
lives beside the delivery requests it reads in
:mod:`src.integration.delivery_truth` because the reduced train must not
import this module. Admission asks the wider
:func:`~src.integration.delivery_truth.undeliverable_repairs_on`, which also
withholds a repair whose own task is terminal, and the train releases an open
batch carrying one rather than let it hold a target against the very source it
was filed for. Only the write side stays here.

### `src/integration/cancelled_collection_recovery.py`

Reopen a parent collection that ``cancel-preserving`` stopped mid-episode.

Historical cancellation ended a repair operation. When the
operation is a parent's *collection* operation -- the one
``ParentEpisodeRecords.reserve_episode_on`` created for the parent's current
episode -- nothing collects that parent any more.  Its checkpoint still says
``awaiting_children`` in the original episode, its completed children's
receipts are bound to ``(operation, episode)``, the collector's reservation is
released, and every path that could continue refuses (2026-10-02,
calm-grove-25 and azure-vault-92):

* the collector and ``aq integration redrive-child`` need an ``active`` or
  ``escalated`` operation for the episode;
* ``ParentEpisodeRecords.reserve_episode_on`` returns the episode's existing
  (cancelled) operation, and one operation per episode is a unique index;
* a later child's conflict cannot open a repair, because
  ``RepairService.start`` continues only a live stage whose delegate is still
  inside its deadline.

A replacement episode cannot inherit the receipts either: accepting a receipt
into a new episode requires a verified previous aggregate.  So
:class:`CancelledCollectionRecovery` reactivates the operation in place, in its
original episode, which leaves every receipt valid as recorded.

It is dry-run first and refuses anything it cannot prove quiet: an unresolved
promotion, resolution, ref mutation or attestation (an ambiguous Git push is
never replayed), a branch owner that is not a released or detached
reservation, a stage delegate that is unsettled, attached or locks a
workspace, a verifier, an operator hold, more than one conflict, a conflict
for a child that has moved on, and a parent branch whose remote tip is not the
recorded head.  Applying needs the remote head the dry run reported and a
reason, and within one transaction under the project lock:

* reclaims the collector reservation for the same operation at the next fence
  token, so nothing still carrying an old token (a queued delivery) can write;
* returns the operation to ``active`` (stage 0) or ``escalated``;
* when exactly one current conflict exists, opens a fresh repair stage bound to
  it, with no writer, a new deadline and its own attempt budget.  Ordinary
  dispatch then files a *new* delegate ``repair-<operation>-<stage>``: archived
  delegates are never restored and their cancelled stages stay cancelled;
* returns a parent the exhausted repair terminally BLOCKED to PAUSED, exactly as
  the shared parent transition requires.

With no conflict waiting, the operation resumes with its cancelled stage still
active; ``RepairService.start`` gives the next conflict a fresh stage past it.
The durable Subject retries a reopened stage whose dispatch failed, whatever
the frozen policy's ``on_exhausted``.

Gates, manual holds and the root's human review are never touched; open human
gates on the parent are reported.  Every apply writes an
``integration.collection_reopened`` event.

An escalated, terminal no-progress incident can use the same control for a
later conflict that no old stage owns. This additional path requires the
exact detached collector fence, preserves every old stage, and rechecks Git
under the apply lock. It never retries an exhausted conflict with a new budget.

### `src/integration/owner_recovery.py`

Release an integration branch owner whose writer is gone, preserving its work first.

An ``integration_branch_owners`` row in ``attached`` or ``handoff_pending``
fences its branch for one writer.  Several paths end a writer without
releasing that row (spec §1, G1–G7): delegate retirement and
``cancel_preserving`` keep it by design, a failed transfer leaves it
``handoff_pending``, archive/delete forget it, and slot reuse destroys the
proof every exit-time confirmer needs.  The row then pins the branch, the
writer's claim and its workspace lock forever.

:class:`OwnerRecovery` decides from facts that stay checkable after the fact,
never from the task row or its status:

1. **The writer is gone.**  The row's session is stopped in ``state`` and
   ``desired_state`` and the session provider confirms it by a fresh probe
   (a row naming no session has no writer), and no live session names the
   owning task.  Otherwise ``writer_live``.
2. **The branch is safe.**  After ``git fetch origin`` in the repository's
   base checkout, every worktree holding the branch (or the row's own
   checkout, detached) is inspected.  One a live session works in refuses
   with ``checkout_in_use``; a running pool session with no task and no claim
   in flight is idle there, not working. On a ``worker`` row only work proved against the
   recorded task origin, claim time and matching slot sentinel is eligible for
   preservation; integration rows rely on their fenced writer binding. A missing
   local task ref never borrows a detached slot's HEAD. Uncommitted work is
   snapshotted through a temporary index -- the working tree and the live index are never touched
   -- and anything origin does not already carry (a dirty tree, a HEAD or a
   local branch ahead of or diverged from origin and not on the default
   branch) is pushed, never forced, to the task branch itself. Only a
   divergent tip uses a temporary ``aq/recovery/<owner-row-id>/<sha>`` ref,
   consumed and deleted when the retry merges it into the task branch. The
   branch is then detached in place, without reset or clean.  A fetch or
   push failure refuses with ``origin_unreachable``.
3. **Release in one transaction.**  A compare-and-swap on
   ``(id, fence_token, handoff_state)`` (``stale_fence`` when it misses)
   releases the row with a fresh fence, unlocks the row's workspace
   (disabling it when dirty or retaining an unproven detached checkout, so
   allocation cannot recycle it) and releases
   the gone writer's claim, then writes the audit row and
   ``integration.owner_recovered``.

Every non-dry run writes one ``integration_owner_recoveries`` row.  Refusals
emit ``integration.owner_recovery_refused``; a refusal repeating the latest
recorded reason within :data:`REFUSAL_THROTTLE_SECONDS` is not recorded again,
and the task hears about each refusal reason once.  A dry run pushes,
detaches and records nothing and lists what it would do in
``evidence["planned"]``.  ``no_push`` releases without publishing: nothing is
proved, snapshotted or pushed, every unpublished source stays where it is
(listed in ``evidence["unpublished"]``) and the row's checkout is retained.

There is no force: a row the check refuses stays refused, with its reason.

See docs/superpowers/specs/2026-09-21-integration-owner-recovery-design.md §3.

### `src/integration/stale_owners.py`

Release the stale integration owners a project's drain is waiting on.

``aq integration release-stale-owners`` (``integration_release_stale_owners``)
is the bulk, project-scoped form of ``release-owner`` for the rows that
command cannot take: ``reserved`` branch-owner rows, which name no writer,
left behind when a train run's tasks finished, were archived or were deleted.
Every ``integration_branch_owners`` row that is not ``released`` counts as
active integration work; disabling delivery preserves these reservations.

A row is released only when it is provably safe:

* it is ``reserved`` and names no session or workspace.  An ``attached`` or
  ``handoff_pending`` row is reported: it names a writer, and
  ``release-owner`` owns it, with its stop proof and preservation;
* its owner is finished -- a task that is COMPLETED or FAILED, archived or
  deleted, or an integration operation that ended -- and nothing still acts
  for it: no live session, workspace lock, in-flight candidate ref mutation
  or running operation, and no hierarchy/train project owns the row
  (:func:`src.integration.finished_owners._blocker`, the questions the
  finished-owner maintenance source asks);
* nothing still relies on its fence: no active integration batch lists the
  branch and no unsettled promotion intent names it.  A promoted batch whose
  cleanup is pending keeps its integration branch's owner, because cleanup of
  that local ref checks the owner's fence -- run ``retry-cleanup`` first.  Its
  members' rows are not kept: their work is on the default branch, and
  cleanup refuses to delete a source ref whose owner is not released;
* its work is on origin, proved after one ``git fetch --prune`` of the
  repository: the branch tip is an ancestor of the default branch, or the
  branch is gone and its owner is terminal (COMPLETED, archived, deleted, or
  an ended operation -- a FAILED task may be retried, so a gone branch proves
  nothing for it); and no worktree or local branch holds work origin lacks.

Everything else is reported with its reason and left alone.  A release is the
compare-and-swap ``release-owner`` makes: ``released`` with a fresh fence, one
``integration_owner_recoveries`` audit row naming the principal, an
``integration.owner_recovered`` event and a note on the owning task.  A dry
run fetches but writes nothing.  A released row is never selected again, so
the command is idempotent.

It also drops the project's integration lease when the lease has expired and
its batch is finished (``integration.stale_lease_released``): the drain counts
a lease row however long ago it expired.

### `src/integration/candidate_baseline.py`

The target's own checks are the baseline a red candidate is judged against.

A red candidate is not automatically the batch's fault.  A required check that
also fails on the target commit the candidate was built on is a pre-existing
failure. Normally only a failing check the target does not fail is repairable.
An open batch sealing its own epic subtree is the exception: its retained
candidate can repair proven inherited failures because the seal prevents ordinary
target repair. The ordinary repair budget and publication gates still apply.

The target's verdict for its exact sha comes from the same per-commit check cache
as the candidate's, so the comparison is over rows written by one trusted producer
under one required-check version.  A target with no final verdict for its sha
cannot decide anything: the baseline is requested and observed once before it is
classified, and checks it has still not decided are unproven rather than assumed
to be the batch's.

A required check marked MISSING on the target is not an observed failure. The
target may never have run the required workflow, as with a hand-pushed main whose
only push workflow is attestation. Such a target has no comparable baseline, so
the candidate's failing checks remain repairable. Missing candidate checks still
fail the required set, and publication still requires trusted exact-candidate
success.

An unproven failure is never repaired. An epic with only proven pre-existing
failures may receive one ordinary default-branch sync repair when the current
default head is train-produced or attested and its own trusted exact-head checks
pass those failing names. Otherwise the candidate's failing check suites are
re-requested under bounded exponential backoff, exactly as
source-CI infrastructure handling is, and the bound names the
``candidate_pre_existing_failure`` blocker for a human instead of filing repairs
forever.  The counters are durable per exact candidate, so a repaired or moved
candidate starts from zero, like its repair lineage.

Comparison is a property of the checks provider and the exact target's evidence,
not of the batch: hosted checks can compare targets that ran the required checks,
while local validation jobs only ever exist for candidates. A lane whose provider
cannot compare a target keeps filing repairs exactly as before.

Epic refreshes use their frozen default-branch input as the baseline. The epic
head can already contain regressions and cannot prove a failure pre-existed on
the default branch.
