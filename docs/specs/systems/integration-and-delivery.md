---
tags: [system-spec, integration, delivery, train, ci, repair, promotion, releases]
---

# System spec: integration-and-delivery

**System:** `integration-and-delivery`, one of the 18 systems in the Agent Queue system
inventory (vault: `projects/agent-queue/specs/2026-10-20-agent-queue-system-inventory.md`,
review `rev-bright-rapids`, approved on 2026-10-20).
**Audited:** 2026-10-22 against `origin/dev` at `52d2efe4c`, task
`noble-ridge-97`. Originally written on 2026-10-20 for `eager-vault-55.7`.
**What this page is:** a map of the shipped system for an engineer new to Agent
Queue. Runtime behavior below describes this checkout, not the operator's live
configuration or the current state of remote branches and tasks.

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
- It **repairs** a red or conflicting candidate through one bounded fixer session per batch family. Exhaustion reports once
  to the project supervisor and defers the batch with a finite revisit.
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
| `src/integration/` | Live train, Git evidence, promotion, cleanup and retained compatibility/recovery boundaries; see the [module catalog](../../reference/modules/integration.md). |
| `src/integration/train.py`, `train_sources.py`, `batches.py`, `train_controls.py` | **The live delivery engine.** `IntegrationTrain`, target and batch selection, the batch store, and the operator controls. |
| `src/integration/gitops.py`, `regeneration.py`, `lock.py`, `checks.py`, `ci.py`, `attestation.py`, `candidate_baseline.py`, `fixers.py`, `fixer_runtime.py`, `failure_groups.py` | Train helpers: deterministic merging, generated-file regeneration, the branch lock, required-check evaluation, trust manifests, hosted attestation, pre-existing-failure baselines and repair allocation. |
| `src/integration/service.py` | `IntegrationService`, the daemon's integration loop. |
| `src/integration/delivery_truth.py`, `git_truth.py`, `delivery_observer.py`, `provenance.py` | Delivery truth: completion provenance refs, Git-based containment proofs, and the observer that the scheduler, archive and status read. |
| `src/integration/promotion_steps.py`, `promotion_routing.py`, `src/commands/promote_commands.py` | Promotion flows and steps. |
| `src/integration/cleanup.py`, `branch_retirement.py`, `branch_sweep.py`, `pr_cleanup.py`, `owner_recovery.py` | Cleanup, retirement and recovery. |
| `src/integration/root_pull_requests.py`, `github_review_poll.py`, `epic_delivery.py`, `stacked_branches.py` | Root and epic PRs, the PR gate, the read-only epic delivery projection, and stacked prerequisites. |
| `src/commands/integration_commands.py` | Every `integration_*` command, about 70 of them (section 3.4). |
| `src/commands/ci_commands.py` | `ci_completed_status`, `ci_baseline_status` (the `ci-main-sentinel` playbook's observation half) and `ci_repair_adopt`. |
| `src/cli/integration.py`, `src/cli/promote.py`, `src/cli/promote_rulesets.py` | `aq integration …` and `aq promote …`. |
| `src/doctor/integration_checks.py` | the `integration.*` doctor checks. |
| `src/install/deploy.py`, `update.py`, `deploy_history.py` | The deploy half of releases (`aq update`). |
| `src/releases/notes/` | Release notes, one file per version. |
| `src/prompts/integration_playbooks/` | Shipped policy (`fast-smooth-train`, `promotion-continuous`, `promotion-request`) and retained older playbooks. |
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

The Git-first train is always constructed by `train_for()`. The compatibility
setting `integration.git_first` defaults to `active` and accepts only `active`.
Old `shadow` configurations fail validation with a migration message: remove the
key or set it to `active` before restarting. Legacy subjects are retained
historical evidence, never an alternate delivery engine.

The current project activation supplies train policy (`train_policy.py`). Its
binding is `hierarchical_integration_policy.train.playbook_id`, defaulting to
`fast-smooth-train`. Explicitly disabling the bound project activation stops train
work; an unavailable, unhealthy or unreadable bound artifact produces finite retry.
Projects with neither a binding nor an activation keep pre-install behavior.
Current playbook required checks overlay a pending candidate without changing its
frozen sources or intent. See the [train runbook](../../guides/git-first-train-runbook.md).

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
  epic, development or promotion (`train.py`). No trains table exists; a target
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
- **Repair.** A bounded session working on the retained red or conflicting candidate
  (section 3.3.3).
- **Receipt.** An append-only `task_delivery_receipts` row with a disposition of `code`,
  `noop`, `ineligible`, `skipped` or `failed`. The legacy protocol wrote receipts as its
  record of a landing. **The train writes none** (section 6).
- **Delivery truth.** "Is task T delivered to ref R?" is answered from Git: the task's
  recorded commit is an ancestor of R. Only immutable, OID-bound proofs are cached.
  `delivery_truth.py` and `git_truth.py` hold the two implementations, and
  `delivery_observer.py:DeliveryObserver` serves the answer (section 3.3.6).
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

1. `IntegrationService` (`service.py`) runs its own asyncio loop with a 5 s interval; readiness events also wake it. It is
   wired in `src/orchestrator/core.py`, together with
   `IntegrationOutbox`, `IntegrationAttestationService`, `IntegrationCleanupService`,
   branch materialization, retirement and discard, and `TrainCommandDriver`.
2. Each `tick` runs `_reconcile` in three parts: the train, then any injected compatibility runtimes (none in current daemon wiring), then
   maintenance. Maintenance has a 300 s budget per source and 60 s per item. The tick
   then dispatches the outbox. With a train attached, the outbox delivers only
   `ACTIVE_OUTBOX_EVENT_TYPES`: the `promotion.*` and `batch.*` events.
3. Maintenance callbacks include:
   - Git-truth reconciliation
   - orphaned parent operations
   - batch cleanup
   - branch discard and materialization
   - owner recovery
   - `RepairService.retire_terminal_delegates`
   - `GitHubReviewPoller`
   - `RootPullRequestReconciler`
   - delivered-PR cleanup

   Maintenance has no delivery authority.
4. `TrainCommandDriver` (`train_sources.py`) calls the train through
   `CommandHandler`, honouring the rule that every state change goes through a command.
   The command is `integration_train_tick`
   (`integration_commands.py:_cmd_integration_train_tick`). It accepts only the
   `integration-train` service principal and is excluded from the HTTP API. Every 60 s it
   runs `stall_alerts.alert_stalled_batches`, and then it calls `IntegrationTrain.tick`.
5. `IntegrationTrain.tick` (`train.py`) visits targets with
   `REPOSITORY_CONCURRENCY = 4`, a 900 s timeout per visit, and a 60–900 s backoff when
   GitHub rate-limits.

#### 3.3.2 From completed work to a green, published candidate

Each visit to a target (`_visit_target`, then `DatabaseBatches.open_batch`, then
`_visit_batch`) does the following:

1. **Fetch once and select sources.** `train_sources.py` drops withheld sources, checks
   that stacks are valid (`stacked_branches.py`) and applies the root PR gate
   (`github_review_poll.RootPullRequestGate`).
   - A leaf root gets its PR at worker close and must pass that PR's checks and review.
     After verifying that the PR head equals the retained source SHA, hosted root
     admission may fill a required check absent from the PR event with its push-event
     check on that exact SHA. An existing PR check takes precedence, including failed
     or unfinished attempts. Both events retain the trusted producer and latest
     workflow-attempt validation; other events and other SHAs cannot substitute.
     This admission evidence has a separate cache identity from strict PR checks and
     candidate push checks. Conflict and review requirements still apply.
     A bot refresh that retains an earlier admission source keeps strict PR checks
     for that earlier SHA; push fallback requires the checked SHA to be the PR head.
     A source whose PR checks are red is admitted only together with its authorized
     repair ([conflicting-source batches](../design/conflicting-source-batches.md),
     [root PR check recovery](../design/root-pr-check-recovery.md)).
   - Stacked sources whose prerequisite is not ready are refused with
     `undeclared_stack_base` or `stack_prerequisite_pending`.
2. **Seal when the target is idle.** Eligible work seals immediately; work arriving
   while a batch runs belongs to the next batch. Deprecated `cadence_seconds` and
   `settling_cap_seconds` no longer gate admission; the latter remains a stall-alert
   threshold. `seal-now`
   requests an immediate visit rather than bypassing a cadence gate.
3. **Freeze and merge.** `gitops.py:merge_sources` merges the frozen members
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
5. **Publish the candidate.** `LeasedPublish` (`train_sources.py`) pushes the
   candidate ref under the branch lock with an expected-old compare-and-swap.
6. **Check.** CI runs on the candidate ref. Which refs trigger CI is described in
   [integration CI boundaries](../../guides/integration-ci-boundaries.md), for the current workflow triggers and evidence boundary. The verdict follows section 3.2.
7. **Green.** `settle_on` (`train_sources.py`) fast-forwards the target to the
   candidate with a fenced compare-and-swap push. The lifecycle becomes `promoted`, and
   `cleanup_settled` materializes and advances the cleanup items.
   - A root target also needs the daemon's hosted **attestation** check run
     (`attestation.py`; policy `hosted_attestation` is on for roots and off for epics).
   - An epic target settles through `EpicCompletions.settle`, which opens
     the epic's root PR.

#### 3.3.3 Red and conflicting batches

The daemon constructs `BatchFixerService`, not the task-per-attempt allocator.
`fixers.py` registers one session before launch, retains ambiguous launch outcomes,
and sends changed evidence to that session. A repair uses no new task or pool claim.
The fixer publishes only its bound candidate through `aq integration batch-fix
--action status|push|rebind`; session identity, branch fences and exact-head checks
remain authoritative.

`RepairPolicy` bounds the primary budget (default 1800 s, at most three rounds).
Exhaustion, failed launch or a stopped fixer produces one supervisor incident,
retains the checkout and input, and releases the lane with a finite revisit
(default 300 s). It creates neither an automatic human hold nor a successor agent.
Exact green can still settle the batch after exhaustion.

Attributed failure groups can split the failing subset and its transitive
dependents from independent members. Companion/successor batches retain family
identity and reuse the same fixer. Unknown attribution retains the group.

`CandidateBaselineService` distinguishes introduced failures from target failures.
An epic first tries default-branch synchronization. A sealed epic candidate can
repair proven inherited failures in place, including through the guarded
`repair-target` control. Unknown baseline evidence does not authorize this
exception. Root pre-existing failures receive bounded rechecks; a red promotion
is held. With a bound train policy, a missing push run is a finite wait rather
than a code repair (the unbound compatibility path can allocate a repair).

  Zero in `primary_seconds`, `debug_seconds`, `primary_attempts`, or
  `debug_attempts` disables that limit. Unlimited durations store a null stage
  deadline, including in resolution reservations; they never expire or refuse
  publication because of elapsed time. An unlimited primary attempt budget
  stays primary; an unlimited debug budget stays debug after a finite primary
  budget is spent. `source_ci_infra_attempts: 0` keeps retrying cancellation-only
  observations with the configured bounded backoff. Positive limits in frozen
  snapshots keep their existing behavior.

  Task-free batch fixers use the complete configured primary attempt budget
  for distinct red observations. Without a repair policy they have no time or
  round limit. Successive failed source heads can reopen the original branch
  without a lineage cap; exact-head and concurrent-writer guards still apply.
  The main CI sentinel also defaults to unlimited attempts; its
  optional `max_attempts` argument can set a positive limit, and zero disables it.



Source movement, ejection, withheld sources, pause/resume and abort remain fenced
`TrainControls` operations. Existing ordinary repair tasks can still close through
compatibility adapters; their staged primary/debug lifecycle is not the live
allocator. Removed preserved-repair commands do not provide a train recovery path.

See [batch conflict repair starts](../design/batch-conflict-repair-starts.md) and
[the current recovery procedure](../../guides/integration-troubleshooting.md).

#### 3.3.4 Epics and roots

An epic gets its own branch and target.

- Its children's work batches into the epic branch.
- Epic *sync* and *refresh* batches keep the epic current with the default branch. A
  refresh counts as delivered only if the default head is an ancestor of the epic and
  the trees are identical.
- An idle epic lane also examines unfinished children's declared prerequisites. When
  all missing prerequisites are proven delivered to the default branch, it starts the
  existing attested epic refresh and reports `epic_refresh_pending`. The same trigger
  applies when all completed candidates wait only on `prerequisite_pending`. Unknown
  or genuinely undelivered prerequisites do not start a refresh. Existing refreshes,
  including their failed or aborted exact inputs, retain the refresh service's guards;
  ordinary frozen batches keep ownership of the lane.
- Epic checks are advisory by default (`EpicPolicy.checks_required=False`). Only an
  enabled, ready project playbook with `tables.parent_episode.required_checks` adds
  an epic CI gate. Stored parent check settings alone do not add it.
- When aggregate completion and any explicit epic gate are satisfied,
  `EpicCompletions.settle` opens one PR from the epic branch to the default branch.
  It then delivers as a root, with root checks and reviews still required.
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
2. `promotion_steps.py:PromotionVisit` publishes in two recoverable writes:
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

1. when tag deployment is configured, picks the newest annotated tag matching
   `deploy.tag_glob` that is
   reachable from `origin/<deploy.target>`;
2. refuses a lightweight or moved tag, a tag on an unrelated history, and a rollback
   across a breaking migration;
3. records the deployment in `~/.agent-queue/deploy.json` and
   `deploy-history.json`;
4. launches the updater detached.

`deploy.tag_glob` defaults to `None`; set it (for example `v*`) and
`deploy.target` to select tag deployment. With `track_branch` configured instead, the daemon checks every 300 s and runs the update
automatically (`core.py:_check_tracked_deploy`).

The name "release" also means `src/integration/release.py:IntegrationReleaseService`.
That is the legacy "release of a terminal root train", reachable only through the
`integration_release` command. The disabled `root-train` playbook used to call it, and
an active train does not.

#### 3.3.6 Delivery truth for the rest of the system

`DeliveryObserver` answers delivery questions for other systems:

- SQL picks the identities to check.
- `delivery_truth` (or `GitTruth`, `git_truth.py`) proves containment against an
  isolated fetched store, outside any database transaction.
- `DeliveryView.verified_on` and `fresh` let writers re-check before they act. A missing
  proof fails closed: nothing settles, archives or deletes.
- Read surfaces (status, explain, doctor) may reuse a snapshot up to 30 s old
  (`READ_MAX_AGE`). Guarded writers never reuse one.

The consumers are:

| Consumer | What it reads |
|---|---|
| **Claims** (`claim_queries.py`, around lines 631–812) | The frontier excludes proven missing prerequisite delivery or pending refresh: `frontier_sibling_prerequisite_not_delivered`, `frontier_prerequisite_not_on_default_branch` and `frontier_epic_refresh_pending`. Unknown or unloaded observations permit worker preparation. |
| **Readiness** (`src/task_graph/readiness.py`) | Maps delivery exclusions to "Waiting on delivery", reading cached snapshots only. Unknown evidence adds no exclusion. |
| **Archive and delete** (`archive_queries.py`) | Refuse without an observer. |
| **`delivery-of` edges** (`blocked_state.py:delivery_landed`) | Source completion in SQL plus a default-target Git observation in `AdmissionView`; unknown evidence permits preparation without establishing delivery. |

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
  | Batches | `batch-fix`, `repair-target`, `pause-batch`, `resume-batch`, `abort-batch`, `eject`, `seal-now`, `settle-batch`, `record-root-noop`, `record-noop`, `release-withheld-source`, `release-held-gate` |
  | Hierarchy | `refresh-epic`, `reverify-epic`, `redrive-root`, `authorize-root`, `redrive-child`, `recover-completed-container`, `reopen-collection` |
  | Owners and origins | `retire-origin`, `restore-task-origin`, `reserve-owner`, `release-owner`, `release-stale-owners` |
  | Recovery and legacy | `clear-retained-recovery`, `close-delivered-pr`, `supersede-completion`, `reevaluate-repair`, `engine-transfer`, `off`, `migrate-provenance-refs`, `cutover-plan`, `cutover` |

  `aq promote …` and `aq update …` are covered in section 3.3.5. The symptom-to-command
  index is
  [integration troubleshooting](../../guides/integration-troubleshooting.md).
- **Doctor:** integration checks include:
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

All tables are in `src/database/tables.py`; search by table name.

| Group | Tables | Used by the train? |
|---|---|---|
| Batches and candidates | `integration_batches`, `integration_batch_members`, `integration_check_evidence`, `integration_attestation_publications`, `integration_cleanup_items`, `integration_outbox` | Yes |
| Branch authority | `integration_branch_owners` (3386; `UNIQUE(repository_id, ref)`; legacy `owner_id`/`fence_token`/`handoff_state` columns side by side with Git-first `holder`/`fence`/`expires_at`), `task_branch_origins`, `branch_retirements`, `branch_deletion_audit`, `integration_owner_recoveries` | Yes |
| Source evidence | `integration_source_ci`, `integration_review_evidence`, `integration_root_authorizations`, `task_integration_checkpoints` | Partly |
| Receipts | `task_delivery_receipts` (3636; append-only, `UNIQUE domain_key`) | Historical audit/recovery; no `delivery-of` eligibility authority |
| Repair and parents (legacy) | `integration_repair_operations`, `integration_repair_stages`, `integration_repair_stage_evidence`, `integration_parent_episodes`, `integration_child_dispositions`, parent verifications | No (retained) |
| Legacy reconciler and publisher | `integration_subjects`, `integration_subject_journal`, `integration_candidate_*`, `integration_promotion_intents` (3499; root intents only), `integration_root_intent_members`, `project_integration_schedules`, `project_integration_leases`, `integration_release_results`, `integration_legacy_deliveries`, waiver/rollout/gate-applicability tables | No (retained; the drop set is empty) |
| Per project | `projects.hierarchical_integration_mode`, `hierarchical_integration_policy`, `integration_mode`, `integration_repository_id`, `promotion_flow`, `default_branch_cutover` | Yes |

The train holds one live batch per target with a `pg_advisory_xact_lock` in
`batches.py:BatchStore._target_lock`. The unique index
`uq_integration_batches_active_project` covers legacy batches only.

**Configuration:**

- `IntegrationConfig` (`src/config.py`, about lines 2577–2735; restart-required).
  `git_first`, `default_mode` and `merge_*` are covered above.
  - `reconciler_active` (default `True`) is retained for historical ownership guards; it does not select a delivery engine.
  - `reconciler_shadow` and `publisher_stall_after` have no remaining effect.
- Per-project policy is `projects.hierarchical_integration_policy`, validated by
  `models.py:HierarchicalIntegrationPolicy`. Its parts are:
  - boundaries
  - `RepairPolicy`
  - `IntegrationCIPolicy`: `hosted`, `local` or `hybrid`
  - `IntegrationTrainPolicy`: deprecated timing thresholds, optional policy binding and speculative CI
  - `IntegrationCleanupPolicy`

---

## 4. Boundaries and interactions

This table covers the inventory's other 17 systems and where the line falls with each.

| System | Interaction |
|---|---|
| **daemon-core** | Owns the schema, migrations, `run_one_cycle` and its ordering, and wires `IntegrationService` into the orchestrator. Integration's 5 s loop runs beside the cycle, not inside it. |
| **task-graph** | Owns tasks, edges and the blocked projection. Integration supplies delivery truth for `delivery-of` edges and the frontier's delivery fences, retains legacy repair-task compatibility, and restricts graph edits that a train depends on. |
| **scheduling-and-claims** | Owns the frontier query and claims. It calls `DeliveryObserver` for the claim-time delivery exclusions and fails closed without a snapshot. Live batch fixers do not consume claims; retained repair tasks still do. |
| **routing-and-profiles** | Resolves the intelligence class and harness for a batch fixer; retained repair tasks use ordinary routing. |
| **sessions-and-harnesses** | Owns the worker session. A passing close records completion provenance, which is the train's input. `aq git push` publishes the worker's branch. |
| **projects-workspaces-git** | Owns projects, repositories, worktrees and the `GitManager`. Integration keeps its own retained Git stores under the data directory and uses the async Git API. Branch origins (`task_branch_origins`) sit on the line: workspaces create them, and integration retires them. |
| **playbooks** | Holds integration *policy*: promotion-continuous and promotion-request, ci-main-sentinel, and supervisor triage of held batches. Playbooks act through `integration_*` and `promote_*` commands, and the train reads current project train policy during a visit. |
| **supervisors-and-policy** | The supervisor watches stalled batches, deferred fixers and explicit holds and promotion requests, and holds a scoped set of integration controls. The [factory policy](../../concepts/factory-policy.md) is this system's normative contract. |
| **messaging-and-human-asks** | A batch fixer that exhausts its budget reports once to the project supervisor and defers with a finite revisit. Stall alerts and promotion approvals reach the operator through messages and the digest. |
| **reviews-and-knowledge** | Root PR review evidence (`integration_review_evidence`, `RootPullRequestGate`) comes from GitHub, not from document reviews. Approved specs only start the work the train later delivers. |
| **providers** | Fixer sessions and retained repair tasks are subject to provider availability and failover. Integration itself makes no model calls. |
| **configuration-and-vault** | Supplies `integration.*`, `deploy.*` and per-project policy columns. The vault holds the reviewed playbook bundles that carry promotion policy. |
| **api-and-clients** | Integration commands go out through `/api/execute` only. `integration_train_tick` is excluded. |
| **dashboard** | Shows readiness ("Waiting on delivery"), PRs (`/api/reviews/pull-requests`), deployment history and epic delivery state. It computes no delivery state of its own. |
| **test-service-and-jobs** | `LocalChecks` and `HybridChecks` can run required checks as local managed jobs, and `aq test` is what workers run before close. Hosted CI is GitHub Actions (`.github/workflows/tests.yml`). |
| **operations-and-recovery** | Owns the doctor framework, `aq update` mechanics and `aq restart`. Integration contributes 14 doctor checks and the operator recovery commands (owner recovery, completed-container recovery, retained-store recovery). |
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
   generated merge driver and by humans alike.
7. **Mechanism in code, policy in playbooks.** The train makes no LLM calls. Promotion,
   escalation and triage are playbooks and supervisors driving commands.
8. **Every state change goes through `CommandHandler`**, including the train's own tick
   (`integration_train_tick`, service principal only).
9. **Bounded repair with a durable escalation.** Each failure gets one incident, a
   bounded fixer budget, a finite revisit and one supervisor report. An explicit
   human hold is a separate operator decision.
10. **Append-only evidence.** Receipts, audit rows and journals are protected by
    database triggers (`migrations/integration_guards.py`). Recovery preserves
    evidence before it deletes (backup bundles, `aq/preserved/<id>`).
11. **A daemon refusal is an answer.** Nobody uses `gh pr merge` or a direct push to work
    around it ([factory policy](../../concepts/factory-policy.md)).

---

## 6. Remaining limitations and audit findings

- **Engine selection:** `integration.git_first` now defaults to `active` and accepts
  only `active`. Old `shadow` values must be removed or migrated before restart;
  project activation and explicit holds remain binding.
- **Portable policy model:** `src/policy_profiles/models.py:IntegrationSettings`
  omits `development` from its rollout-mode literal, although the project runtime
  accepts that mode. Reported as a suspected code bug; no model change is made here.
- **Retained compatibility:** Subject tables, journals, staged repair adapters and
  owner recovery remain. Their presence does not imply a live Subject publisher.
  Some CLI help still uses Subject/cadence language; current procedures above and in
  the train runbook describe the active engine.
- **JSON columns:** the integration JSON columns noted in the original system map
  are explicitly grandfathered by `tests/test_migration_json_columns.py`. They are
  not evidence of a newly introduced migration violation.

The original 2026-10-20 map included live task states, remote branch tips, release
progress and retrospective latency counts. Those were dated observations, not
system contracts, and are no longer presented as current state. Use `aq promote
status`, integration status and current Git evidence for operational state.

## 7. Features shipped since the original map

The fast-smooth implementation is present in this audited `origin/dev` checkout.
It is no longer future work on a separate epic branch. The current implementation
includes:

- event-driven readiness, durable dispatch waits, stalled-batch diagnostics and
  delivery latency observations;
- immediate idle-target admission, current project playbook policy and advisory
  epic checks;
- failure grouping, task-free batch fixers, retained split/successor identity and
  finite budget-exhaustion revisits;
- warm retained workspaces and build-cache support;
- strict Git containment, current admission evidence and Git-truth reconciliation;
- declared stacked prerequisites, author-assisted close merging and opt-in
  speculative next-batch CI;
- generated-file regeneration once per assembly and retained exact source objects.

The [hierarchical train guide](../../guides/hierarchical-integration-trains.md)
describes the controls, close handoff, cache settings, waits and speculation.
The [module catalog](../../reference/modules/integration.md) points to the shipped
boundaries. A source module does not prove that a feature is enabled in an
operator's project; activation and project policy still apply. Future task/review
states belong in the daemon and vault rather than a static system specification.

---

## 8. Further reading

Current behaviour:

- [Integration](../../concepts/integration.md): the concepts page.
- [Factory policy](../../concepts/factory-policy.md): the normative admit, deliver and
  recover contract.
- [Git-first train runbook](../../guides/git-first-train-runbook.md): the operator's
  procedure.
- [Integration troubleshooting](../../guides/integration-troubleshooting.md):
  symptom-to-command.
- [Promotion flow](../../guides/promotion-flow.md) and [releases](../../guides/releases.md).
- [Hierarchical integration trains](../../guides/hierarchical-integration-trains.md):
  current train controls and explicitly retained recovery procedures.
- [Integration CI boundaries](../../guides/integration-ci-boundaries.md).
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
- Module reference: [docs/reference/modules/integration.md](../../reference/modules/integration.md).

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

### Historical PR #1858 ownership authorization

The following records the original reviewed footprint, rather than a new merged ceiling.

For PR [#1858](https://github.com/ElectricJack/agent-queue/pull/1858), supervisor
decision `msg-08efc39cceeb430cafb8636f1acb85f1` under
`size-cap-delegation-2026-10-23` (operator decision `6679052c`) amends the
recorded transition exception to exactly **161,023 owned lines**, **85,320
engine lines** and **45 tables** at source
`a68665e4afde3be9a0aba0d9dec50e61e632be84`. This adds 186 owned lines and four
engine lines to the previous exact measurement. The ownership records retain
the earlier decisions and the measurement-equality assertion remains enforced.
