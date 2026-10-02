# Integration train mechanism inventory

Date: 2026-10-02. Task: `brisk-cascade`. Source snapshot: `a14c0715e70e112a4352b088a8a1e978c274258d`.

This is a code inventory and a set of review candidates, not authorization to change integration policy. It describes this checkout; installed vault copies, active route bindings, and work on other branches can differ.

Coverage: all **81 Python modules** in `src/integration/` and their **1227 module functions/class methods**, all callables in the dedicated command, contract, CLI, doctor, and six integration-query modules, plus the supporting integration call sites listed below (**1808 callables total**). Constructors, validators, protocols, private helpers, and record methods are included; pure field declarations are covered by their owning contract/service. Nested lexical callbacks are covered by their enclosing method; generic infrastructure unrelated to an integration call site and generated API mirrors are outside this inventory.

Each module heading links to its source file at the recorded snapshot; names below inherit that module prefix; bold class rows set the class prefix for the following methods, until the next class/module row. Crosswalks use those same scoped names; the overlap table links its definition lines. Purposes were checked against function bodies, call/return paths, guards, and docstrings; the coverage check compares the rows with Python AST definitions. A surfaced control, its contract adapter, and its service are different layers of one mechanism, not three independent implementations.

## Navigation

- [Hierarchy and parent collection](#hierarchy-and-parent-collection)
- [Root scheduling, candidates, and promotion](#root-scheduling-candidates-and-promotion)
- [Repair operations, delegates, and handoffs](#repair-operations-delegates-and-handoffs)
- [Ownership, fences, and stopped-writer recovery](#ownership-fences-and-stopped-writer-recovery)
- [CI, source review, trust, and protection](#ci-source-review-trust-and-protection)
- [Development publication and delivery truth](#development-publication-and-delivery-truth)
- [Cleanup, preservation, and removal](#cleanup-preservation-and-removal)
- [Operator recovery, rebind, and rollout controls](#operator-recovery-rebind-and-rollout-controls)
- [Background service, durable events, and supporting types](#background-service-durable-events-and-supporting-types)
- [Command and CLI surfaces](#command-and-cli-surfaces)
- [Durable query and schema support](#durable-query-and-schema-support)
- [Doctor checks](#doctor-checks)
- [Daemon, session, Git, and playbook runtime integration](#daemon-session-git-and-playbook-runtime-integration)
- [Shipped playbook rules](#shipped-playbook-rules)
- [Overlap and simplification candidates](#overlap-and-simplification-candidates)

## Route and loop map

| Mechanism | Entry and continuation | Purpose |
| --- | --- | --- |
| Parent collection | `HierarchyIntegration` → `CollectionService` → `PromotionService` → `ParentCompletion` | Collect reviewed child work under the parent's exact episode/generation and verifier fence. |
| Root train | `IntegrationScheduler` → outbox → `TrainService` → `CandidateService` → CI/attestation → `RootPromotionService` | Freeze reviewed roots, test one candidate, and publish its exact attested head to main. |
| Development delivery | `DevelopmentIntegration.tick/sweep` → `PublisherJobs` → ref-write journal → leased Git push | Publish owed completion generations using project-selected finite validation and per-repository exclusion. |
| Control loop | `IntegrationService._run/tick` | Run scheduling, green continuation, and outbox dispatch while allowing only one remote reconciliation pass. |
| Remote pass | `IntegrationService._reconcile` | Reconcile accepted/retired delegates, owners, branches, reviews, collection, CI, repairs, intents, cleanup, and drains. |
| Development pass | `IntegrationService._reconcile` starts one separate development task | Keep a slow publication/validation job from occupying the shared remote pass. |
| Main scheduler | `Orchestrator.run_one_cycle` | Materialize branches before assignment and backstop delivery-aware settlement/lifecycle cleanup. |
| Ready-owner recovery | `Orchestrator._reconcile_sessions` → `schedule_ready_owner_recovery` | Run one asynchronous recovery pass for reopened tasks and interrupted terminal closes. |
| Durable policy replay | `V2PlaybookRuntime.accept_integration_event` → pending-job reconciliation | Accept pinned destinations durably and resume bounded playbook dispatch after crashes. |

Sources: [src/orchestrator/core.py](../../../src/orchestrator/core.py#L1730), [src/integration/service.py](../../../src/integration/service.py#L84), [src/integration/completion_recovery.py](../../../src/integration/completion_recovery.py#L29), [src/playbooks/runtime.py](../../../src/playbooks/runtime.py#L287).

The wired intent callback handles **root** intents; parent resolution/promotion reconciliation also comes through commands and the parent policy's durable events. Both published task completion and delivery to a target have their own evidence; a close, cached status, or vanished branch is not interchangeable with exact delivery proof.

## Hierarchy and parent collection

### [src.integration.hierarchy](../../../src/integration/hierarchy.py)

| Scoped name | One-line purpose |
| --- | --- |
| `materialize_exact_branch` | Create *branch* at *base_sha*, refusing any unexpected existing tip. |
| `_workspace_precondition` | Build the refusal for one named `resolve_workspace_checkpoint` gate. |
| `resolve_workspace_checkpoint` | Return an owned writer workspace's clean, exactly-pushed current HEAD. |
| `resolve_repair_commit_proof` | Snapshot the exact ordered commits in one proved repair lineage. |
| `resolve_workspace_repair_proof` | Prove a clean pushed writer HEAD and snapshot all commits from its subject. |
| `verify_workspace_checkpoint` | Prove a parent's actual checkout HEAD is clean and exactly pushed. |
| `hierarchy_mode_enabled` | Whether writes for *project* use isolated hierarchical delivery. |
| **`HierarchyIntegration`** | The project-lock writer for checkpoints, origins, and child membership. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `file_children` | File children against the locked parent checkpoint and reserve immutable branch origins. |
| `readiness` | Delegate parent delivery-readiness evaluation to ParentCompletion. |
| `record_disposition` | Delegate the child's explicit disposition receipt to ParentCompletion. |
| `verify_parent` | Check the observed parent head before recording exact parent verification evidence. |
| `complete_parent` | Delegate verified parent completion to ParentCompletion. |
| `wake_verifier` | Delegate wake-up under the exact transferred verifier fence. |
| `checkpoint_leaf_completion` | Advance only a leaf's live completion head after clean pushed proof. |
| `file_prepared_child_on` | Insert a validated command-layer task in the caller's transaction. |
| `file_prepared_children_on` | Insert sibling tasks with one parent-generation advance. |
| `file_root_on` | Insert an enabled-project root and reserve its isolated origin. |
| `bootstrap_container_collection` | Checkpoint an affirmatively untouched container at its pinned origin. |
| `checkpoint_parent` | Prove and record the parent generation/head and reserve its collection episode. |
| `checkpoint_and_suspend_parent` | Atomically reserve the collection episode and pause its producer. |
| `materialize_origin` | Create a pending canonical ref only when absent or already exact. |
| `mutate_hierarchy` | Reparent unmaterialized work and update affected generations and branch origins. |
| `_root_route` | The (project, repo) a new root in *project_id* files through. |
| `graph_route` | The route a task graph in *project_id* files through, or `None`. |
| `_enabled_route` | Require the task's active hierarchical project and designated repository. |
| `_projects_table` | Expose the projects table used by route validation. |
| `_repo_on` | Read and convert a repository row on the caller's connection. |
| `_task_row` | Read a task row or raise the typed hierarchy not-found refusal. |
| `_locked_checkpoint` | Lock and require the task's integration checkpoint. |
| `_locked_origin` | Lock and require the task's unretired branch origin. |
| `reconcile_unmaterialized_tasks_on` | Bind a pristine legacy graph to its designated hierarchy repository. |
| `_ensure_origin_chain` | Reserve the missing immutable origins and checkpoints along the ancestor chain. |
| `_resolve_head` | Resolve and validate an exact repository/branch head through the configured reader. |
| `_reserve_origin` | Reserve an immutable branch base and owner fence and enqueue materialization. |
| `_insert_checkpoint` | Insert the initial generation/head checkpoint for an origin. |
| `_bump_checkpoint` | CAS the parent's expected generation and reset stale verification state. |
| `_write_task_extras` | Persist validated task criteria, context, tools, and labels during filing. |
| `_maybe_create_routing_gate` | Create an assignment-routing gate when the filing policy requires one. |
| `_build_child` | Construct a child with inherited project/repository and refuse worker-authored routing fields. |
| `_assert_reparentable` | Reject hierarchy movement after immutable origins or integration history make it unsafe. |

### [src.integration.parent_completion](../../../src/integration/parent_completion.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`ParentCompletion`** | Conn-owned primitives for one parent's durable collection episode. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `reserve_episode_on` | Freeze a parent episode, operation, policy, routes, and starting collection identity. |
| `readiness` | Evaluate a parent's readiness using a hierarchy-locked context. |
| `mark_ready_on` | Project readiness into checkpoint state and one durable event. |
| `readiness_on` | Require settled child dispositions, exact code receipts, collected head, and verifier evidence. |
| `_trusted_code_receipt` | Accept clean squash edges or the exact conflict-resolution proof shape. |
| `record_disposition` | Record an idempotent delivered/no-code/waived child receipt and recompute readiness. |
| `verify_parent` | Validate exact generation/head and green CI evidence before recording parent verification. |
| `wake_verifier` | Wake only the exact transferred verifier on the collected head. |
| `complete_parent` | Complete a verified parent. |
| `_complete_parent_transition` | CAS the verified parent to COMPLETED and settle its operation without violating holds. |
| `_locked_context_on` | Lock and validate parent, project, checkpoint, and episode operation, allowing exact archive replay. |

### [src.integration.collection](../../../src/integration/collection.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`CollectionService`** | Submit one child per parent; promotion remains the mutation authority. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `tick` | Page paused parents awaiting children and queue collection for each. |
| `collect_parent` | Queue *task_id*'s next approved child now instead of on the next tick. |
| `queue_next` | Choose an approved undelivered child and enqueue an exact fenced parent-promotion event. |
| `return_repaired_branch` | Recover collection only after a completed repair detached and delivered. |

### [src.integration.child_delivery](../../../src/integration/child_delivery.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`_ProofFailed`** | Git does not show the recorded head as the child's published tip. |
| `__init__` | Configure the service’s collaborators and retained state. |
| **Module functions** | Definitions in the linked module. |
| `_snapshot_identity` | Extract the source status, route, base, head, generation, and review identity used for CAS. |
| `_snapshot_on` | Everything that decides whether *task_id* can be assembled; reads only. |
| `_child_refusal` | Why this task is not a completed code child that could be assembled. |
| `_parent_refusal` | Explain why the parent is not the exact active paused collector for this child. |
| `stuck_children_statement` | Completed code children a collecting parent has not assembled. |
| `latest_evidence_on` | The newest review verdict on exactly this child snapshot, if any. |
| `_open_reviewer_on` | An unfinished reviewer task filed about *task_id* (`discovered-from`). |
| **`ChildDelivery`** | Prove a completed child from Git and hand it to its parent's collector. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `ensure_evidence` | Record completion evidence for a completed child lacking a verdict. |
| `_defer` | Record backoff before retrying a child's unproved delivery evidence. |
| `run` | Preview or CAS exact child evidence, reissue a proven receipt if needed, and queue collection. |
| `diagnose` | Report what the child's assembly waits on; writes nothing. |
| `_diagnose` | Read the child/parent/review snapshot and prove source identity and prior receipt ancestry. |
| `_prove` | Return the remote tip and tree of the child's head, or say why not. |
| `_prove_receipt_ancestry` | A reissued receipt must still name code on the live parent branch. |
| `_reissue_receipt` | Acknowledge the same incorporated head for a newer task completion. |
| `_record` | Append approved leaf evidence after re-reading the child under lock. |

### [src.integration.collecting_parent_recovery](../../../src/integration/collecting_parent_recovery.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`CollectingParentRecovery`** | Dry-run-first recovery of a BLOCKED parent with a reserved collector. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `diagnose` | Preview whether a blocked parent can resume its existing collection episode. |
| `_diagnose_on` | Validate the paused-collection checkpoint, active operation, detached collector, and absence of holds. |
| `run` | CAS a proven blocked collector back to PAUSED without changing its episode or fence. |

### [src.integration.epic_branch](../../../src/integration/epic_branch.py)

| Scoped name | One-line purpose |
| --- | --- |
| `slugify` | Return a lowercase hyphenated slug, truncated on a word boundary. |
| `reserve_branch_name` | Reserve this task's branch in its repository; never change an existing name. |

### [src.integration.epic_dependencies](../../../src/integration/epic_dependencies.py)

| Scoped name | One-line purpose |
| --- | --- |
| `declare` | Record an epic dependency without changing an earlier declaration. |
| `dependencies_for` | Return dependencies for the requested epics only. |
| `dependents_of` | Return live epics that declared a dependency on this epic. |
| `order_members` | Place dependencies first; defer members with absent or cyclic blockers. |

### [src.integration.epic_pr](../../../src/integration/epic_pr.py)

| Scoped name | One-line purpose |
| --- | --- |
| `leaf_root_source` | Classify a childless root's checkpoint as a PR source. |
| `render_body` | Describe the epic and carry its stable identity in a GitHub trailer. |
| **`EpicPullRequestService`** | Create one GitHub pull request and retain its URL on the root task. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `open_for_epic` | Open/reuse the exact completed root's PR with dependency trailers, skipping heads already on main. |
| `_source_rows_on` | The root's live checkpoint and origin, either of which may be absent. |

### [src.integration.root_materialization](../../../src/integration/root_materialization.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`RootMaterialization`** | Prove a PR and its Git lineage before recording a legacy leaf root. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `run` | Prove a completed legacy train root's PR, base, and head and reserve its missing canonical origin. |
| `_state` | Require a completed root with the designated train repository, canonical branch, and PR identity. |

### [src.integration.root_authorization](../../../src/integration/root_authorization.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_identity_conditions` | Build exact-source SQL conditions for an operator root authorization. |
| `exact_root_authorization_on` | The grant for exactly this source, or `None`. |
| **`RootAuthorization`** | Dry-run first, head-fenced, idempotent recording of one exact grant. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `run` | Preview or grant reviewed-root eligibility for one exact source head with an audit reason. |
| `_project_of` | Resolve the live root task's owning project. |
| `_state` | Classify one task; `candidate` carries the exact source to record. |

### [src.integration.root_pull_requests](../../../src/integration/root_pull_requests.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_has_children` | Test for live or archived children when distinguishing root leaves from epics. |
| `pr_ready_roots_statement` | Completed train roots with no PR whose checkpoint names a finished head. |
| **`RootPullRequestReconciler`** | Retry the PR a completed train root's completion path failed to open. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `tick` | Page completed PR-ready train roots and attempt each deferred PR publication. |
| `_page` | Read the next bounded PR-ready root page. |
| `open_one` | Open an exact root PR through EpicPullRequestService and back off failures. |
| `_defer` | Schedule capped exponential PR-open retry backoff for one root. |
| **`RootDeliveryRedrive`** | Diagnose one completed train root and re-drive its missing PR. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `run` | Preview or requeue an eligible root's PR/delivery, delegating blocked collection recovery when applicable. |
| `diagnose` | Report what the root's delivery waits on; reads only. |

## Root scheduling, candidates, and promotion

### [src.integration.scheduler](../../../src/integration/scheduler.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`IntegrationScheduler`** | Coalesce periodic and manual triggers into one durable sweep request. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `maintain_lease` | Refresh only the outstanding batch's exact authority before dispatch. |
| `configure` | Persist scheduling controls while retaining any outstanding request. |
| `mark_due` | Mark one sweep due, or return the durable request already in flight. |
| `_release_stale_request_on` | Release an outstanding request nothing will end; see `stale_schedule`. |
| `_maintain_batch_lease_on` | Keep the active request fenced independently of its next sweep interval. |
| `_result` | Serialize sweep-request scheduling outcome and outstanding request identity. |
| **`TrainService`** | Inspect reviewed sources, then seal the compatible frontier atomically. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `seal` | Retry moved observations and seal at most one exact reviewed frontier under a project lease. |
| `_seal_once` | Prove sources outside SQL, reselect them under lock, and freeze batch members/policy/operation. |
| `_record_migration_deferral_on` | Record the exact source whose colliding migration head deferred train sealing. |
| `_eligible_members` | Select and dependency-order exact reviewed roots without bypassing holds or repair-chain eligibility. |
| `_batch_for_request` | Read the batch bound to one durable project sweep request. |
| `_replay_result` | Return the durable sealed/empty answer for an existing request batch. |
| `_consume_request` | Consume only the exact outstanding request id and sequence. |
| `_batch_id` | Hash project and request into the deterministic batch id. |
| `_integration_branch` | Name the batch's private integration branch from project/request digests. |
| `_source_ref` | Require and normalize a source branch into a valid heads ref. |
| `_manifest_digest` | Hash canonical frozen batch-member JSON for manifest identity. |
| `_result` | Serialize train sealing outcome, request, batch, and operation. |

### [src.integration.stale_schedule](../../../src/integration/stale_schedule.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`OutstandingRequest`** | Typed record including `project_id`, `verdict`, `reason`, `request_id`, `request_sequence`. |
| `as_dict` | Serialize the outstanding request classification and its durable identities. |
| **`RequestRelease`** | Typed record including `request_id`, `catchup_request_id`, `lease_released`, `schedule`. |
| `as_dict` | Serialize released request/lease identity and any catch-up request. |
| **Module functions** | Definitions in the linked module. |
| `classify_outstanding_request_on` | Classify *schedule*'s outstanding request inside the caller's transaction. |
| `_unsealed_verdict_on` | Distinguish a live sweep event from a stale request that never sealed a batch. |
| `_foreign_lease` | Report a project lease held by a different batch. |
| `_write_blockers_on` | Evidence that a remote write for *batch_id* may still be in flight. |
| `release_outstanding_request_on` | Release the request *state* classified, in the caller's transaction. |
| `release_stale_request` | Classify, and unless *dry_run* release, one project's outstanding request. |
| `release_ended_batch_request` | After *batch_id* ended without shipping, free its request if nothing else will. |

### [src.integration.settling](../../../src/integration/settling.py)

| Scoped name | One-line purpose |
| --- | --- |
| `note_approval` | Arm or extend a project's window without moving its first-approval cap. |
| `settled` | Delay only an armed window; ordinary periodic sweeps keep running. |
| `clear` | Disarm the window after a batch seals or is abandoned. |

### [src.integration.candidates](../../../src/integration/candidates.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`MergeConflictError`** | The member overlap could not be resolved by generation or absorbed by driver. |
| `__init__` | Retain merge diagnostics on the candidate construction exception. |
| **Module functions** | Definitions in the linked module. |
| `_merge_evidence` | The merge-tree diagnostic from a failed `merge-tree` result, if any. |
| **`CandidateConstraintError`** | The database refused a candidate row itself, not because a writer won a race. |
| `__init__` | Retain the named durable-constraint violation on the authorization exception. |
| **`AuditForgeProvider`** | Define idempotent audit-PR lookup and creation interfaces. |
| `lookup_audit_pr` | Define the provider interface for finding an audit PR by idempotency key. |
| `create_audit_pr` | Define the provider interface for creating a candidate audit PR. |
| **`_MutationObservation`** | Typed record including `blocker_ids`, `recoverable_ids`. |
| `blocks_build` | Report whether unresolved writes block candidate construction. |
| `has_unresolved` | Report either blocking or recoverable candidate writes. |
| **Module functions** | Definitions in the linked module. |
| `_mutation_reason` | Format the unresolved ref-mutation ids that prevent construction. |
| **`CandidateService`** | Build every immutable member into one candidate revision. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `build` | Build a batch candidate and guarantee a durable continuation for unfinished work. |
| `_build` | Validate authority, reconcile writes, construct the candidate, and publish its audit ref/PR. |
| `_fetch_construction_base` | Retain the observed-main construction base of a continuous candidate. |
| `_withdraw_invalid_sources` | End a batch whose frozen manifest holds a Git-proven unbuildable source. |
| `_ancestry_observation_on` | Bind an ancestry rejection to the frozen member and current source task. |
| `_live_delegates_on` | Detect a live task session or attached owner blocking batch withdrawal. |
| `_has_reviewed_ancestry` | Prove the reviewed base is an ancestor of the reviewed source head. |
| `rebuild` | Create a successor candidate revision at new main and continue construction. |
| `_continued` | Never end a live batch's construction without a durable next step. |
| `_schedule_construction_retry` | Queue an idempotent retry event for the current unfinished candidate. |
| `_rebuild` | Fence a successor revision, preserving accepted repairs and reconciling pending writes. |
| `_preserve_ci_repair` | Merge an accepted CI-repaired candidate with main, preserving both histories. |
| `_continue_rebuild_conflict` | Persist and dispatch an exact main/candidate conflict without new budget. |
| `reserve_repair` | Freeze an exact candidate repair from the current instance-bound writer. |
| `accept_repair` | Prove a pushed repair, transfer its stopped writer's fence, and accept it into the candidate. |
| `recover_repair` | Resolve a pushed reservation from the LOCAL recovery surface only. |
| `_retire_superseded_repair` | Preserve an old private push after its stopped writer was released. |
| `_arm_terminal_recovery` | Open a new bounded stage for the exact terminal repair reservation. |
| `_reject_pushed_repair` | Retain one invalid pushed proof only after its writer is no longer live. |
| `_accept_repair_result` | Commit the accepted repair lineage and current candidate/member result. |
| `_reserve_repair_handoff` | Atomically transfer stopped repair ownership and reserve the collector's candidate write. |
| `push_repair` | Push a reserved private repair using its exact session, lease, and branch fence. |
| `_repair_state` | Lock and validate the active repair delegate, session instance, workspace, and reservation. |
| `_canonical_workspace_path` | Validate and canonicalize the reservation's workspace path. |
| `_locked_state` | Lock the batch and current revision, validate its lease, and obtain collector ownership. |
| `_owner_reason_on` | Describe the current integration-branch owner that blocks construction. |
| `_return_completed_repair_on` | Recover a closed delegate's detached branch for the collector. |
| `_ensure_revision` | Create or read the initial candidate revision under current authority. |
| `_construct` | Merge frozen reviewed members in order and durably checkpoint each applied member. |
| `_merge_with_generated_fallback` | Produce the merged tree; when overlap is confined to generated artifacts, resolve the driver conflict in a scratch worktree and regenerate the affected artifacts before returning. |
| `_publish` | Journal the candidate ref push and create or reconcile its exact audit PR publication. |
| `_pending` | Record the member's pending construction identity before doing external work. |
| `_applied` | Commit an applied member's head and advance the revision ordinal. |
| `_validate_authority_on` | Revalidate project mode, batch lease, revision, stage, and branch fence under lock. |
| `_mutation_id` | Derive a stable ref-mutation id from its complete immutable identity. |
| `_mutation_identity` | Build the journal identity for candidate, repair, or collector ref writes. |
| `_reserve_mutation_on` | Insert or reuse the exact fenced mutation claim and reject conflicting identities. |
| `_mutate_ref` | Reserve, perform, and reconcile one exact authenticated ref mutation. |
| `_authority_is_current` | Recheck candidate authority in a hierarchy-locked transaction. |
| `_takeover_expired_mutation` | Reclaim an expired unmarked write only after validating current authority. |
| `_prepush_authorized` | Check both mutation nonce and current candidate authority immediately before push. |
| `_mutation` | Read one candidate ref-mutation journal row. |
| `_reconcile_observed_mutation` | Persist whether the exact remote observation proves a reserved write applied. |
| `_observe_unresolved_mutations` | Inspect unresolved remote writes and separate blockers from recoverable build work. |
| `_recoverable_build_mutation` | Decide whether an unresolved build write belongs to the current recoverable authority. |
| `_accepted_parent_repair` | Replay an accepted member repair only from its validated older reservation lineage. |
| `_reserved_lineage` | Build CandidateRepairLineage from the immutable reservation fields. |
| `_conflict` | Freeze the first exact member conflict, publish the partial candidate, and dispatch bounded repair. |
| `_valid_repair_lineage` | Whether a frozen repair has the one permitted Git shape. |
| `_record_reviewed_file_guard` | Record the repair's reviewed-file guard observation as durable evidence. |
| `_repair_lineage_failure` | Prove the repair's exact commit range and reviewed source ancestry, allowing authorized file changes. |
| `_batch_lineage_failure` | Authorize the complete aggregate through frozen ancestry and first-parent work. |
| `_blob` | Read a blob's object identity from the retained candidate repository. |
| `_commit_exists` | Check that the retained store contains a commit object. |
| `_member_identity_matches` | Prove a member's reviewed head, base ancestry, and tree identity. |
| `_repair_result` | Serialize a candidate repair result with its frozen lineage. |
| `_resolution` | Read a candidate repair reservation on a fresh connection. |
| `_resolution_on` | Read one candidate repair reservation on the caller's connection. |
| `_member_result` | Read the durable construction result for an exact member and revision. |
| `_revision` | Read an exact candidate revision. |
| `_has_live_mutation` | Detect a pending candidate write or publication preventing revision replacement. |
| `_repository` | Resolve the configured repository or fail if it is unavailable. |
| `_assert_repository_binding` | Require the authenticated App binding to name the candidate's repository. |
| `_ensure_store` | Atomically initialize the retained candidate bare store in a private directory. |
| `_fetch_oid` | Retain a valid exact commit locally without treating object presence as freshness proof. |
| `_fetch_inputs` | Fetch the frozen batch base and every reviewed source head. |
| `_integrator` | The batch project's resolved commit identity (git-identity spec). |
| `_pin` | Retain a candidate/source object at a daemon-owned local ref. |
| `_retain_sources` | Pin available frozen member heads for later audit and recovery. |
| `_crash` | Invoke the injected crash hook at a durable reconciliation boundary. |
| `_recovery_ref` | Name the retained candidate ref by batch digest and revision. |
| `_authors` | Extract commit authors and coauthor trailers from the reviewed source range. |
| `_message` | Render an integration merge message with source and review provenance and attribution. |
| `_result` | Serialize the candidate build outcome and exact candidate revision/head. |

### [src.integration.promotion](../../../src/integration/promotion.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`PromotionConflict`** | Typed refusal inheriting `PromotionError`. |
| `__init__` | Retain the durable conflict intent identity on the merge-conflict exception. |
| **`PromotionService`** | Prepare once, push by exact lease, and recover receipts by ancestry. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `observe_legacy_resolution_target` | Read the exact remote target for an operator's legacy recovery. |
| `prepare` | Prove reviewed source and target, construct a squash commit, and freeze its promotion intent. |
| `push` | Push a prepared parent promotion under the exact branch fence and expected target head. |
| `reserve_resolution` | Freeze a repair writer's exact resolution before any remote mutation. |
| `recover_unwritten_resolution` | Create a fresh successor after proving a bad reservation never wrote. |
| `push_resolution` | Push only a previously frozen resolution under the current repair writer. |
| `_resolution_push_scope_on` | Require the exact attached repair writer and current intent before a resolution push. |
| `_record_resolution_push_on` | Persist authenticated observation of the reserved conflict-resolution push. |
| `reconcile` | Classify an interrupted parent promotion from actual remote reachability or resolution evidence. |
| `_reconcile_resolution` | Prove the frozen resolution on the remote target and commit its delivery receipt. |
| `_assert_exact_resolution` | Prove a linear resolution range preserves the old target and reviewed source tree. |
| `_resolution_commit_range` | Read and validate the ordered exact commit range for a resolution. |
| `_validated_route` | Resolve the child's current parent, canonical branch, immutable base, and repository route. |
| `_validated_context` | Require current exact-head review evidence for the resolved delivery route. |
| `_get_repo` | Resolve the canonical repository via the configured reader. |
| `_resolve_repository` | Bind canonical repository, authorized origin URL, and retained bare store path. |
| `_ensure_retained_repository` | Initialize or verify the retained bare store and its frozen origin. |
| `_fetch_all_heads` | Fetch repository heads into the retained promotion store. |
| `_assert_remote_source` | Require the source's remote branch to remain at the reviewed head. |
| `_assert_remote_target` | Require the target's exact remote head to remain at the expected base. |
| `_assert_commit_inputs` | Validate source/base/target objects and source ancestry before preparing a squash. |
| `_is_ancestor` | Run strict Git ancestry proof, raising on an indeterminate command result. |
| `_assert_object_type` | Require the retained Git object to have the expected type. |
| `_tree_oid` | Resolve and validate the exact commit tree identity. |
| `_authors` | Collect author and coauthor identities from the source commit range. |
| `_add_identity` | Normalize and deduplicate a commit author/coauthor pair. |
| `_message` | Render the squash commit message with task, review, and author attribution. |
| `_provenance` | Freeze command principal and pinned playbook artifact provenance into the intent. |
| `_domain_key` | Hash the full source, target, operation, and fence identity for idempotent promotion. |
| `_assert_existing_request` | Reject replay whose immutable request differs from the existing intent. |
| `_clean_tree_oid` | Validate the clean merge-tree output's sole tree identity. |
| `_conflict_diagnostics` | Bound merge-conflict paths and diagnostics without losing valid JSON structure. |
| `_pin_recovery_ref` | Retain the exact prepared commit at its immutable intent recovery ref. |
| `_prepared_reachable` | Prove the prepared commit equals or is an ancestor of the observed target. |
| `_finalize` | Commit the promotion's receipt/checkpoint projection and read the durable answer. |
| `_intent` | Require the durable promotion intent to exist. |
| `_assert_frozen_repository` | Require the retained origin URL to match the intent's frozen repository identity. |
| `_assert_resolution_repository` | Require the resolution workspace/repository to match frozen intent authority. |
| `_repair_subject_matches_intent` | Accept only the conflict's old tip or its frozen resolution tip. |
| `_value` | Serialize the intent, receipt id, and prepared SHA as PromotionValue. |
| `_crash` | Invoke the injected crash hook at a durable reconciliation boundary. |

### [src.integration.main_promotion](../../../src/integration/main_promotion.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`RootPromotionConstraintError`** | The database refused the root reservation, and no canonical intent explains it. |
| `__init__` | Attach the named root-promotion constraint violation to the invariant error. |
| **`RootAttestationProof`** | Live CI receipt identity, or legacy App publication proof, supplied by Task10. |
| `subject` | Extract the immutable subject fields from the root-attestation proof. |
| **`RootPromotionService`** | Reserve all root receipts and the one fenced exact-main mutation. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `prepare` | Validate exact green authority and freeze one root intent, member receipts, and main write journal. |
| `readiness_on` | Whether the current revision is promotable now; reserves nothing. |
| `_reconciliation_blocker_on` | An in-flight attestation or main intent that must settle first. |
| `promote` | Prepare the exact root promotion, then reconcile its durable main intent. |
| `reconcile` | Observe main, refresh authority/attestation, perform the leased push, and finalize proven delivery. |
| `_claim_execution` | Claim a root-intent execution nonce only under the current batch/owner authority. |
| `_mark_prewrite` | Record main's irreversible-write marker after rechecking proof and authority deadlines. |
| `_mark_applied` | Mark the exact main mutation applied with its observed remote head. |
| `_supersede_unattempted` | Supersede an unwritten stale-base intent and durably enqueue candidate rebuilding. |
| `_intent_matches_authority` | Compare frozen intent lease, stage, revision, and owner fields with current authority. |
| `_finalize_root` | Commit exact delivery receipts, checkpoints, batch/operation settlement, and cleanup continuation. |
| `_mutation` | Read the root intent's durable main ref-mutation row. |
| `_receipt_ids` | Read the ordered source receipt ids frozen by the root intent. |
| `_blocked` | Return an unresolved root-reconciliation result with intent and receipt identity. |
| `_is_current_revision` | Compare a revision with the batch's authoritative current revision. |
| `_is_ancestor` | Probe commit ancestry and preserve an unknown Git result as unknown. |
| `_local_fast_forward` | Prove the prepared candidate contains the intent's expected main head. |
| `_import_observed_main` | Fetch the exact observed main commit for reconciliation proof. |
| `_store` | Name the retained bare integration repository by canonical repository digest. |
| `_snapshot` | Read promotion state under the project hierarchy lock. |
| `_snapshot_on` | Read batch, candidate, operation, stage, lease, owner, publication, and aggregate evidence. |
| `_validate_snapshot` | Return the refusing outcome from the shared promotion snapshot guard. |
| `_snapshot_blocker` | `(outcome, reason)` refusing promotion now, or `None` when promotable. |
| `_attestation_subject` | Construct the candidate's exact root-attestation subject from publication and policy. |
| `_proof_matches_state` | Require every attestation subject field to match current candidate state. |
| `_resolve_attestation` | Revalidate the supplied proof through the configured resolver. |
| `_frozen_attestation` | Decode the attestation proof frozen in intent provenance. |
| `_pin_recovery` | Retain the prepared candidate at the intent's stable local recovery ref. |
| `_repository` | Resolve the batch repository or raise a root-promotion invariant error. |
| `_client_for` | Resolve/cache a repository-bound authenticated App client. |
| `_project_id` | Resolve a batch's owning project before acquiring its hierarchy lock. |
| `_intent` | Read an exact root promotion intent on a fresh connection. |
| `_intent_on` | Read an exact root promotion intent on the caller's connection. |
| `_existing_result` | Read the result of an already frozen root intent. |
| `_existing_result_on` | Validate replay identity and serialize the intent's prepared/promoted answer. |
| `_identity` | Derive the deterministic domain key and root-intent id for a batch revision. |
| `_receipt_id` | Derive the stable source receipt id by batch, revision, and member ordinal. |
| `_mutation_id` | Derive the stable main-ref journal identity for a root intent. |
| `_crash` | Invoke the injected crash hook at a durable reconciliation boundary. |

### [src.integration.green_continuation](../../../src/integration/green_continuation.py)

| Scoped name | One-line purpose |
| --- | --- |
| `promotion_fingerprint` | Hash every authority a root promotion binds; fences are monotonic. |
| `_dedup_prefix` | Name one continuation generation by batch, revision, and promotion fingerprint. |
| `continuation_rows_on` | Return the continuations already emitted for one fingerprint, oldest first. |
| `enqueue_green_continuation_on` | Enqueue one idempotent green continuation on the caller's transaction. |
| **`GreenPromotionReconciler`** | Re-drive exact-green root batches whose promotion wakeup was lost. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `tick` | Page green candidates and enqueue missing promotable continuations without repeating unchanged blockers. |
| `candidate_batches` | Exact-green awaiting-completion root batches with no root intent. |
| `_candidate_page` | Select a fair bounded page of current green candidates awaiting promotion. |
| `reconcile` | Advance one batch at most one step; every result names its reason. |
| `_green_deliveries_on` | Read green continuation outbox deliveries for the exact candidate fingerprint. |
| `_report` | Log a batch's continuation state once per change, not once per tick. |

## Repair operations, delegates, and handoffs

### [src.integration.repair](../../../src/integration/repair.py)

| Scoped name | One-line purpose |
| --- | --- |
| `repair_subject_sha` | The exact commit a repair stage's current subject is anchored on. |
| `_unfinished_candidate_publication` | Built candidates retain collector authority through audit publication. |
| **`RepairService`** | Own atomic stage clocks and evidence-accounting transitions. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `pending_dispatches` | Replay incomplete continuous-policy handoffs through the command path. |
| `retire_terminal_delegates` | Settle unfinished delegates whose owning operation has already ended. |
| `reconcile_accepted_delegates` | Recover stopped accepted delegates using the shared acceptance proof. |
| `reserve_batch_operation_on` | Reserve Task8's one pinned, no-stage operation in its transaction. |
| `start` | Activate or durably continue an operation's bounded repair stage. |
| `_resolve_parent_conflict_trigger_on` | Resolve a frozen operation-key alias to one exact conflict intent. |
| `continue_current_parent_conflict_on` | Continue a resumed parent repair from its one current conflict. |
| `_continue_parent_stage_on` | Rebind one detached delegate to a later conflict without new budget. |
| `record_result` | Record one exact check attempt under the current stage budget. |
| `_escalate_stuck_batch_on` | Notify the supervisor once after three consecutive counted failures. |
| `_successor_for_closed_writer` | Advance to a bounded successor stage when the current repair writer already closed. |
| `missing_delegate_reservations` | Read detached current delegates that cannot pass owner admission. |
| `reconcile_delegate_reservations` | Retry interrupted handoffs without restarting the repair clock. |
| `reserve_delegate` | Recover only the delegate already bound to the current stage. |
| `dispatch` | Create and safely hand off to the exact current repair writer. |
| `complete_delegate` | Atomically close one exact attached repair writer and enqueue its fact. |
| `expire` | Conditionally expire the exact current stage at its absolute deadline. |
| `_redrive_unfinished_construction_on` | Re-enter construction once before escalating a stage with nothing to repair. |
| `due_stages` | Return one bounded page of current stages whose deadline is due. |
| `resume_root_collection` | Return an unclaimed debug reservation to its root collector. |
| `return_green_delegate_branch` | Return a closed writer's branch to the collector of its exact green candidate. |
| `return_green_delegate_branch_on` | Hand off under the caller's project lock; `None` when not exactly eligible. |
| `adopt_batch_repair_on` | Bind a proved CI repair while the caller holds the exact writer fence. |
| `bind_current_batch_subject_on` | Bind Task9's authoritative current revision without resetting its budget. |
| `record_batch_rebuild_conflict_on` | Freeze a conflicting main advance under the current root repair budget. |
| `bind_current_parent_subject_on` | Bind a transaction-proved parent HEAD without resetting stage budget. |
| `_root_success_is_current_on` | Check that stage success still names the authoritative green candidate revision. |
| `_effective_dispatch_stage_on` | Map the frozen parent artifact's stage zero to a continued stage. |
| `_dispatch_context_on` | Lock the current operation/stage and derive its exact writable branch target. |
| `_reuse_verifier_on` | Reuse an eligible existing verifier rather than file a duplicate repair writer. |
| `_is_primary_writer` | Whether *owner* is a predecessor writer in one of *states*. |
| `_writer_role_matches` | Map stage writer kinds to their permissible owner roles. |
| `_retained_debug_handoff` | Fence a stopped primary and atomically retain its dirty workspace. |
| `_restore_archived_delegate_on` | Recover a legacy archive of this still-active stage's exact delegate. |
| `_delegate_task_matches` | Require the delegate's exact project, repository, branch, and standalone task identity. |
| `_predecessor_matches` | Check whether a branch owner is the operation's expected collector or verifier predecessor. |
| `_delegate_description_on` | Read current conflict evidence and render the delegate's instructions. |
| `_delegate_description` | Render stage dossier, source lineage, conflict details, budgets, and completion instructions. |
| `_current_batch_subject_rows_on` | Lock and require the operation's authoritative batch and current revision. |
| `_batch_subject` | Build the exact batch revision/candidate SHA subject. |
| `_initial_dossier_on` | Freeze the starting subject, checks, route, receipts, trigger, and finite repair policy. |
| `_dossier_with_evidence` | Append exact attempt evidence and failing-check diagnostics to the stage dossier. |
| `_current_receipts_on` | Read the parent's delivery receipts for repair provenance. |
| `_dossier_with_repair_commits` | Append proved repair commit ranges without treating an unproved tip as lineage. |
| `_activate_debug_on` | Create the finite debug successor stage or escalate unchanged-head exhaustion. |
| `_exhausted_subject_conflict_on` | Return the sole unresolved parent conflict whose old tip is `subject`. |
| `_supervisor_recovery_on` | End an unchanged-head budget once; preserve its writer and incident. |
| `_human_block_on` | Mark the operation human-required, block its parent if applicable, and emit the incident. |
| `_operation_project_id_on` | Resolve operation scope from its batch or live/archive parent identity. |
| `_start_context_on` | Resolve the frozen boundary policy, route, and current repair subject. |
| `_evidence_matches` | Match CI evidence exactly to the operation's parent or batch subject. |
| `_result_value` | Serialize repair-attempt outcome, next action, and counted attempts. |
| `_timeout_value` | Serialize the exact stage deadline outcome and next action. |
| `_dispatch_value` | Serialize delegate dispatch identity, writer kind, and optional fence. |
| `_start_value` | Serialize stage start outcome, anchor SHA, deadline, and operation identity. |

### [src.integration.accepted_repair](../../../src/integration/accepted_repair.py)

| Scoped name | One-line purpose |
| --- | --- |
| `accepted_candidate_on` | Prove the *current* accepted candidate and its detached collector fence. |
| `complete_accepted_delegate` | Close only the original uninterrupted claim; leave CI and ownership alone. |
| `reconcile_stopped_accepted_delegates` | Recover accepted writers stopped by the old timeout path, never a live writer. |

### [src.integration.delegate_release](../../../src/integration/delegate_release.py)

| Scoped name | One-line purpose |
| --- | --- |
| `retired_delegate_message` | Why a delegate cannot be *action*-ed, and what to run instead. |
| `retired_writer_close_feedback` | What a retired delegate's writer is told when its close is refused. |
| `_decoded` | A `task_metadata` value; older rows may hold an unencoded string. |
| `_owned_by` | The OR of every way *operation* can own the task in `task_id_column`. |
| `_role_of` | Which delegate seat *task_id* occupies in *operation_id*. |
| `_stranded_statement` | Delegates of an ended operation that have not settled and have no writer. |
| `stranded_delegates` | Report the delegates `release_delegates` would settle. |
| `_disposition` | Classify ended-operation delegates as cancelled or superseded. |
| `release_delegates_on` | Settle stranded delegates on a caller-owned transaction. |
| `release_delegates` | Open a transaction, run `release_delegates_on`, then notify. |
| `live_integration_owner` | The first still-running operation that owns any task in *task_ids*. |
| `assert_obsolete_delegate_on` | Prove a generated delegate can leave the graph without releasing authority. |
| `archive_obsolete_delegates` | Reconcile a bounded page; all safety proofs are repeated by archive_task. |

### [src.integration.repair_progress](../../../src/integration/repair_progress.py)

| Scoped name | One-line purpose |
| --- | --- |
| `batch_recovery_progress` | Read durable stop/preservation evidence and prove its frozen batch lineage. |

### [src.integration.published_repair_handoff](../../../src/integration/published_repair_handoff.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_snapshot_on` | Read exact authority under hierarchy/row locks; no caller evidence is trusted. |
| `confirm_published_pool_repair_handoff` | Detach only the exact published reservation and CAS its claim-bound owner. |

## Ownership, fences, and stopped-writer recovery

### [src.integration.ownership](../../../src/integration/ownership.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`BranchOwnership`** | Serialize writers using a monotonic database fence. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `get_owner` | Return the current durable owner snapshot for command validation. |
| `acquire` | Reserve an unowned branch; expiry never overrides an attached owner. |
| `_acquire_on` | Insert a first owner or CAS a released row to a new monotonic fence. |
| `transfer` | Transfer only after the old writer's server-side handoff is proven. |
| `confirm_transfer` | Persist handoff intent, then obtain server-side stop evidence outside SQL. |
| `transfer_confirmed_on` | Consume exact external handoff proof in the caller's local transaction. |
| `transfer_detached_on` | Transfer a detached reservation alongside the caller's eligibility checks. |
| `assert_current` | Raise unless *fence* remains the current write authority. |
| `mutation_exclusion` | Hold the ownership row across one bounded external mutation. |
| `mutation_exclusion_on` | Recheck and hold ownership inside a caller's canonically ordered transaction. |
| `attach` | CAS-bind a reserved task owner to its durable writer identity. |
| `_attach_on` | CAS the reserved owner to the exact task/session/workspace/agent writer identity. |
| `begin_startup_reconciliation` | Fence a stale STARTING writer before reconciliation releases it. |
| `_claim_released` | Advance the released row's fence and bind its next owner and role. |
| `_locked_row` | Lock the repository/ref's unique durable owner row. |
| `_validate_identity` | Require nonempty owner identity and a supported integration owner role. |
| `_fence` | Convert the owner row into its exact branch/owner/token fence. |
| `_require_current` | Reject a released, missing, or mismatching owner fence. |

### [src.integration.owner_recovery](../../../src/integration/owner_recovery.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`RecoveryOutcome`** | One row's verdict and the evidence it was reached on. |
| `to_dict` | Serialize owner recovery outcome, reason, evidence, and dry-run state. |
| **`_Refusal`** | Typed refusal inheriting `Exception`. |
| `__init__` | Retain the typed owner-recovery refusal and its evidence. |
| **`OwnerRecovery`** | Prove a branch owner's writer gone and its branch safe, then release it. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `recover` | Run the check on one owner row and release it when it passes. |
| `recover_many` | `recover` each row in turn. |
| `candidates` | Recoverable rows whose writer is not live and that have been quiet. |
| `_row_identity` | The commit identity of the project that owns *row*'s branch. |
| `_load` | Read the exact owner row under a transaction. |
| `_prove_writer_gone` | Exclude successor/live attachments and prove the exact session stopped through its provider. |
| `_secure_branch` | Inspect retained work, snapshot dirty changes if needed, and preserve all heads on origin. |
| `_checkout_for` | Base checkout, default branch, row checkout, authorized repository URL. |
| `_live_work_dirs` | Read canonical work directories still used by attached sessions. |
| `_mutex` | Acquire the checkout's shared Git mutex when available. |
| `_dirty` | Check tracked and untracked checkout changes with porcelain status. |
| `_on_origin` | Does origin already carry *sha*: on the branch, or on the default branch? |
| `_snapshot` | Commit HEAD plus every change and untracked file, leaving the checkout as is. |
| `_commit` | A preservation commit, dated from its first parent so a rerun is identical. |
| `_push_preserved` | Put every sha in *shas* on origin's *ref*, never forcing; return its tip. |
| `_release` | CAS the proven owner fence, record preservation evidence, and release safe claim/workspace state. |
| `_unlock_workspace` | Release the stopped writer's workspace while retaining dirty checkouts from slot reuse. |
| `_release_claim` | Release the gone writer's claim, after the owner row stopped protecting it. |
| `_audit` | Append durable owner-recovery evidence bound to the old fence and principal. |
| `_record_refusal` | Persist a nonreleasing recovery attempt and a task comment with its exact refusal. |
| `_comment` | Append an attributed task comment for owner recovery evidence. |
| **Module functions** | Definitions in the linked module. |
| `owner_recovery_for` | The daemon's `OwnerRecovery`, or `None` when it cannot be built. |
| `_agent_live_elsewhere` | Does *agent_id* run a live session other than *session_id*? |
| `_project_for` | The owning task's project, else the one project integrating the repository. |
| `_same_path` | Compare canonical filesystem paths. |
| `_release_comment` | Render the recovery result and retained-work evidence for the task comment. |
| `_refusal_comment` | Render the typed recovery blocker for the task comment. |

### [src.integration.completion_recovery](../../../src/integration/completion_recovery.py)

| Scoped name | One-line purpose |
| --- | --- |
| `schedule_ready_owner_recovery` | Keep network probes off the scheduler and permit only one recovery pass. |
| `stop_ready_owner_recovery` | Cancel and await the orchestrator's outstanding ready-owner recovery pass. |
| `reconcile_ready_integration_owners` | Repair ended attachments before reopened tasks retry their slot setup. |
| `reconcile_closed_integration_owners` | Fence out stopped writers whose ordinary close already freed their slot. |
| `recover_completed_pool_claims` | Public recovery for a terminal pool close interrupted before cleanup. |
| `recover_completed_pr_links` | An explicit flush repairs exact root PR links lost by older close paths. |

### [src.integration.canonical_reservation](../../../src/integration/canonical_reservation.py)

| Scoped name | One-line purpose |
| --- | --- |
| `reserve_canonical_task_branch` | Reserve only a detached READY/BLOCKED producer's persisted branch. |

### [src.integration.stale_owners](../../../src/integration/stale_owners.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`StaleOwnerRelease`** | Release a project's provably safe `reserved` owners and its expired lease. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `run` | Report every unreleased owner of *project_id*; release the safe ones. |
| `_branch_proofs` | `{row id: (proof or refusal reason, detail)}` from one pruned fetch. |
| `_refs` | Read the fetched origin branch tips from the retained checkout. |
| `_prove` | Require detached safe work and prove the stale tip reached default or its remote ref is gone. |
| `_release_stale` | The `release-owner` compare-and-swap, re-proved under the project lock. |
| `_stale_leases` | Release the project's lease when it expired and its batch is finished. |
| **Module functions** | Definitions in the linked module. |
| `stale_owner_release_for` | The daemon's `StaleOwnerRelease`, or `None` without a DB and Git. |
| `_kept` | Serialize a nonreleasing stale-owner outcome with its refusal detail. |
| `_row_blocker` | The owner's state when nothing in the database keeps *row*; else refuse. |
| `_owner_status` | `COMPLETED`… for a task, `archived`, `operation:<state>`, `deleted`. |
| `_fence_blocker` | Refuse while a batch or promotion intent still relies on the row's fence. |

### [src.integration.finished_owners](../../../src/integration/finished_owners.py)

| Scoped name | One-line purpose |
| --- | --- |
| `stop_confirmer_for` | The daemon's provider-backed stop probe, or `None` outside the daemon. |
| `finished_branch_owners` | Every non-released task-owned row whose owner finished or is gone. |
| `release_finished_branch_owners` | Release every row `finished_branch_owners` clears, and return them. |
| `_scan` | Read candidate owner rows and apply provider-backed stopped-writer checks outside SQL. |
| `_evaluate` | Classify one row; `None` when its owner has not finished. |
| `_blocker` | Reject release when scope, a live session/workspace, or operation still protects the owner. |
| `_live_task_session` | A session that may still write for *task_id*, or `None`. |
| `_stopped_writer` | The attached row's writer session, or why its stop cannot be proven. |
| `_stop_proof_blocker` | Require the daemon's session provider to confirm the exact writer stopped. |
| `_project_ids` | Find projects designating the owner row's repository for integration. |
| `_public` | Serialize the owner finding, stop proof, eligibility, and blocker. |

### [src.integration.drain_owners](../../../src/integration/drain_owners.py)

| Scoped name | One-line purpose |
| --- | --- |
| `terminal_reservation_clause` | SQL predicate for an owner whose fenced work has durably finished. |

## CI, source review, trust, and protection

### [src.integration.ci](../../../src/integration/ci.py)

| Scoped name | One-line purpose |
| --- | --- |
| `is_numeric_producer_id` | Whether a policy `producer_id` is the canonical numeric App id. |
| **`SubjectTrustError`** | A subject tree's trust manifest is absent, oversized, malformed or names another identity, so the subject is refused (spec I4, I6). |
| `__init__` | Attach a typed subject-trust refusal code to the attestation error. |
| **`RequiredChecksManifest`** | Typed record including `version`, `names`. |
| `unique_nonempty_names` | Require unique, nonblank required-check names. |
| **`IntegrationCITrust`** | Repository and producer identity derived from frozen integration policy. |
| `valid_producer_identity` | Require a nonempty producer id and canonical decimal form for numeric ids. |
| **Module functions** | Definitions in the linked module. |
| `ci_trust_from_policy` | Derive CI trust from one frozen project-policy boundary, without a repo manifest. |
| **`IntegrationTrustManifest`** | Typed record including `schema_`, `canonical_repository_id`, `repository_id`, `full_name`, `ci_producer_app_id`. |
| `distinct_apps` | Reject an attestation App that is also its own CI producer. |
| **`AttestationPayload`** | Typed record including `schema_`, `canonical_repository_id`, `repository_id`, `ci_producer_app_id`, `attestation_app_id`. |
| `coherent_attempts` | Validate check/workflow coverage and consistent workflow attempts. |
| `from_canonical_bytes` | Decode a payload only if reserialization matches its exact canonical bytes. |
| `canonical_bytes` | Serialize an attestation deterministically for identity and publication. |
| `external_id` | Hash canonical attestation bytes into the versioned external id. |
| **`CIReceiptPayload`** | Canonical live-GitHub evidence retained by the daemon, not published as a check. |
| `coherent_attempts` | Require checks to be covered by the recorded workflow runs and attempts. |
| `canonical_bytes` | Serialize a daemon CI receipt deterministically. |
| `external_id` | Hash canonical receipt bytes into the versioned receipt identity. |
| **Module functions** | Definitions in the linked module. |
| `select_trusted_attestation` | Select and validate the newest trusted exact-name App attestation. |
| **`IntegrationCIEvidence`** | Typed record including `id`, `operation_id`, `batch_id`, `candidate_revision`, `parent_task_id`. |
| `exact_subject` | Require the evidence's parent or candidate subject fields to be mutually coherent. |
| **`TrustedFixtureObserver`** | Explicit test-only observation seam; production uses authenticated GitHub reads. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `observe` | Return injected test evidence only after matching it against trust authority. |
| **`IntegrationCIEvidenceAdapter`** | Append-only normalized evidence writer that retains the caller transaction. |
| `append_on` | Append immutable exact-subject CI evidence or reuse its matching duplicate. |
| **`CIService`** | Observe typed integration subjects and durably append normalized trusted evidence. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `observe_parent` | Observe authenticated parent CI and append evidence only if its subject remains current. |
| `observe_candidate` | Observe authenticated candidate CI and project exact aggregate success under lock. |
| `_project_candidate_green_on` | Persist the one aggregate success identity consumed by promotion. |
| `_lock_parent_subject_on` | Lock and validate the current parent episode, generation, SHA, and frozen trust. |
| `_lock_candidate_subject_on` | Lock and validate the batch revision, stage, publication, and frozen trust. |
| `_enabled_project_repository` | Require an active hierarchy/train project with the designated repository. |
| `_append_observation_on` | Append per-check and workflow evidence with deterministic exact-subject identities. |
| `_operation_trust` | Derive boundary trust from the operation's frozen policy snapshot. |
| **`AuthenticatedGitHubObserver`** | Build canonical evidence exclusively from authenticated GitHub API reads. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `observe` | Read exact-head check runs and latest push workflow attempts from the trusted producer. |
| `publish` | Publish one completed canonical attestation through the configured App. |
| **Module functions** | Definitions in the linked module. |
| `_require_payload_matches_trust` | Validate payload repository, producer, checks, version, and subject against authority. |
| `_trust_producer_id` | Normalize producer identity from either trust representation. |
| `_payload_producer_id` | Normalize producer identity from either receipt representation. |
| `_check_producer_app_id_matches` | Compare numeric producer policy with the check run's App id. |
| `_producer_matches` | Match a check's numeric App id or permitted legacy producer slug. |
| `_require_workflow_repository` | Require the workflow run to belong to the trusted numeric repository. |
| `_require_latest_attempt_jobs` | Verify required jobs belong to the latest recorded workflow attempt. |
| `_validate_check_workflow_coverage` | Reject checks without a matching workflow run and coherent attempt. |
| `_reject_duplicate_keys` | Reject duplicate fields while decoding canonical attestation JSON. |
| `_strict_int` | Accept integers while rejecting booleans masquerading as ids. |

### [src.integration.candidate_ci](../../../src/integration/candidate_ci.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`CandidateCIService`** | Build unfinished candidates and observe/attest their exact CI subject. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `handle` | Resume an unpublished candidate build, then observe and attest its exact CI subject. |

### [src.integration.parent_ci](../../../src/integration/parent_ci.py)

| Scoped name | One-line purpose |
| --- | --- |
| `ensure_parent_store` | Initialize the retained bare repository used for immutable parent CI snapshots. |
| `publish_parent_snapshot` | Create an immutable CI ref; a conflicting remote ref is never overwritten. |
| **`ParentCIService`** | Publish parent CI refs, observe frozen checks, and enqueue exact results. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `tick` | Page parent verification subjects and observe each independently. |
| `handle` | Publish an immutable parent CI ref, observe exact frozen checks, and enqueue its result. |

### [src.integration.source_ci](../../../src/integration/source_ci.py)

| Scoped name | One-line purpose |
| --- | --- |
| `classify_source_checks` | Judge the latest trusted run of each required check, including cancellation. |
| `pull_request_conflicts` | Whether GitHub reports *pull* as unmergeable because of a conflict. |
| `observe_source_ci` | Read source PR checks/conflict state under frozen policy and invoke the source-CI handler. |
| `repair_description` | Render source-CI repair instructions with exact PR, head, checks, and failures. |

### [src.integration.source_ancestry](../../../src/integration/source_ancestry.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`SourceAncestryObservation`** | Server-observed proof that one exact train source identity is unbuildable. |
| `from_evidence` | Rebuild the observation a recorded ancestry rejection was written from. |
| `identity` | Return the frozen task, repository, base, head, and generation identity. |
| `matches` | Compare the observed rejection with the source's current immutable identity. |
| **`SourceAncestryInvalid`** | Approval refused: Git proved the source's recorded base is not its ancestor. |
| `__init__` | Configure the service’s collaborators and retained state. |
| **Module functions** | Definitions in the linked module. |
| `describe` | Explain a proved source-base ancestry or reviewed-tree mismatch. |
| `repair_feedback` | Instructions a worker can act on without rewriting reviewed history. |
| `rejection_evidence_id` | Derive a stable exact-source ancestry-rejection review evidence id. |
| `prove_member_identity` | Return `(reason, merge_base, tree)` for one frozen batch member. |
| `merge_base_of` | Return a proved Git merge base, leaving failed observations unknown. |

### [src.integration.review_evidence](../../../src/integration/review_evidence.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`ReviewEvidenceProducer`** | Resolve graph identity and pin one server-observed Git verdict. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `snapshot_from_pull_request` | Observe and freeze an epic's exact pull-request source for review evidence. |
| `snapshot_authorized` | Pin task authorization without impersonating a human review. |
| `_authorization_on` | Require eligible root kind or exact operator authorization plus gates and review constraints. |
| `_snapshot_pull_request` | Store a GitHub verdict against the exact verified train epic head. |
| `_prove_base_ancestry` | Refuse approval unless Git proves the recorded base is an ancestor. |
| `record_ancestry_rejection_on` | Append the exact rejection that withdraws an unbuildable identity. |
| `ancestry_rejection_on` | The ancestry rejection that is the exact identity's latest verdict, if any. |
| `_pull_request_source_on` | Read the exact root/epic branch, checkpoint, and PR source identity. |
| `snapshot` | Precompute immutable Git facts; return None for legacy projects. |
| `complete_review_on` | Revalidate the frozen subject, append its verdict, and atomically complete the reviewer. |
| `reject_and_reopen_on` | Append exact rejected review evidence and reopen only its unchanged source task. |
| `_append_on` | Append immutable review evidence and reject conflicting duplicate identities. |
| `_revalidate_on` | Lock and require the subject's current route, generation, head, and review binding. |
| `_subject` | Resolve a unique review subject from final-review blocking or discovered-from edges. |

### [src.integration.github_review_poll](../../../src/integration/github_review_poll.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`GitHubReviewPoller`** | Read a bounded page through AQ's configured repository credential. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `tick` | Page completed train-root PRs and poll each with bounded retry handling. |
| `_page` | Select a fair page of roots with PRs requiring current review/source evidence. |
| `_poll` | Route existing ancestry rejections or observe the current PR for exact review evidence. |
| `_repair_ancestry` | Invoke the command-backed source-ancestry repair continuation when configured. |
| `_observe` | Read authenticated exact-head PR review/CI state and record approved/rejected source evidence. |

### [src.integration.attestation](../../../src/integration/attestation.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`IntegrationAttestationService`** | Resolve exact live CI into a durable receipt; retain legacy App publication. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `publish` | Reserve and publish an exact green candidate's App attestation or daemon CI receipt. |
| `resolve` | Revalidate a claimed proof against current candidate state and authenticated CI records. |
| `handle_candidate_ci` | Observe a pending candidate, then publish its exact attestation. |
| `enablement_blockers` | Check repository identity and availability of the configured CI observer/client. |
| `_load_trust` | Bind frozen policy authority to the authenticated client and subject-tree identity. |
| `_subject_manifest` | Read the trust manifest from the exact subject tree, classifying refusals. |
| `_record_subject_trust` | Remember one refused subject for `aq integration status` (spec §5.3). |
| `subject_trust_blockers` | Status blockers for refused subjects that are still current (spec I6). |
| `_subject_still_current_on` | Check whether a recorded trust refusal still names the active parent or candidate. |
| `_client` | Resolve and cache an App client whose credential identity matches the binding. |
| `_green_state` | Require an exact matching green candidate and completion-ready stage. |
| `_pending_state` | Read an exact pending candidate still eligible for CI observation. |
| `_enqueue_candidate_result` | Emit one exact-current durable continuation from authoritative evidence. |
| `_publication_id` | Derive the stable attestation-publication id from batch and revision. |
| `_publication_identity` | Extract the immutable candidate and evidence fields for publication. |
| `_reserve_publication` | Claim or reconcile a durable publication lease without transferring a marked write. |
| `_mark_publication_prewrite` | Record the publication's irreversible-write marker under current subject authority. |
| `_finish_publication` | Commit the check-run proof under the exact publication nonce and subject. |
| `_published_publication` | Read an already published record only while its subject remains current. |
| `_publication_subject_current_on` | Lock and validate batch, candidate, operation, stage, and green evidence identity. |
| `_locked_state` | Read the locked candidate subject, frozen checks, publication, and aggregate evidence. |
| `_subject_matches` | Compare every immutable root-attestation subject field with current state. |
| `_attestation_records` | Fetch all exact-name check runs for the subject commit through the App. |
| `_trusted_records` | Select exact-name check runs produced by the configured attestation App. |
| `_proof_from_records` | Construct a root proof from the selected canonical App attestation. |
| `_proof_from_observation` | Construct a daemon receipt proof only for the recorded aggregate external id. |
| `_publish_daemon_receipt` | Persist a publication proof backed by authenticated aggregate CI evidence. |
| `_store` | Name the retained bare integration repository by canonical repository digest. |
| `_crash` | Invoke the injected crash hook at a durable reconciliation boundary. |
| `_project_reader` | Collect project-reader/credential blockers from the configured probe. |
| **Module functions** | Definitions in the linked module. |
| `_parse_trust_manifest` | Validate the subject-tree JSON manifest and classify identity versus syntax refusals. |
| `_reject_duplicate_keys` | Reject duplicate manifest fields during JSON decoding. |

### [src.integration.hosted_attestation](../../../src/integration/hosted_attestation.py)

| Scoped name | One-line purpose |
| --- | --- |
| `verify` | Return an inert or successful/failed hosted verdict from configured attestation identities. |
| `_verify_configured` | Read the exact App attestation, validate its canonical payload, and freshly reverify checks. |
| `_paged_check_runs` | Fetch bounded check-run pages while validating every pagination destination. |
| `_newest_trusted` | Select the newest exact-name check run from the configured attestation App. |
| `_canonical_payload` | Validate payload shape, canonical bytes, and its hash-bound external id. |
| `_require_shape` | The `AttestationPayload` model's constraints, so nothing the daemon refuses passes. |
| `_require_identity` | Require payload repository, commit, producer, App, and check version to match hosted authority. |
| `_reverify` | Freshly read referenced check runs and workflow attempts and require their exact success. |
| `_require_fields` | Reject missing or unexpected payload fields. |
| `_require_text` | Require a nonempty text payload field. |
| `_require_id` | Require a positive nonboolean numeric payload id. |
| `_require_sha` | Require a valid exact commit SHA in the payload. |
| `_strict_int` | Accept integers while refusing booleans used as numeric ids. |
| `_positive_decimal` | Parse a configured positive canonical decimal App id. |
| `_reject_duplicate_keys` | Reject duplicate payload JSON fields. |
| `_reject_constant` | Reject nonfinite JSON constants during payload decoding. |
| `_json` | Decode JSON and convert malformed responses into hosted verification errors. |
| `_next_link` | The `rel="next"` target of a GitHub `Link` header, if any. |
| **`_RefuseRedirects`** | Never forward the token elsewhere; a redirect is an error, so CI runs. |
| `redirect_request` | Refuse HTTP redirects when reading authenticated attestation evidence. |
| **Module functions** | Definitions in the linked module. |
| `urllib_get` | Build a bounded HTTPS transport that refuses redirects and untrusted destinations. |
| `_one_line` | Printable ASCII on one line, so a reason cannot add `$GITHUB_OUTPUT` keys. |
| `_append` | Append the verdict to a configured Actions output/summary path or stdout. |
| `main` | Render hosted verification status and Actions evidence; enforcement is handled by the workflow. |

### [src.integration.trust_manifest](../../../src/integration/trust_manifest.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`TrustManifestRefusal`** | A manifest cannot be built; `code` is the blocker the preflight uses. |
| `__init__` | Configure the service’s collaborators and retained state. |
| **Module functions** | Definitions in the linked module. |
| `build_trust_manifest` | The manifest object, validated by `IntegrationTrustManifest`. |
| `_required_checks` | Validate the parent/root required-check authority used in the generated manifest. |
| `policy_producer_app_id` | The numeric CI producer both policy boundaries name. |
| `manifest_for_policy` | The manifest a bound (or candidate) policy implies for one repository. |
| `canonical_text` | The committed form: sorted keys, two-space indent, one trailing newline. |
| `text_sha256` | Hash canonical UTF-8 trust-manifest text. |
| `_field` | Resolve a dotted manifest field or return the missing-field sentinel. |
| `_reject_duplicate_keys` | Reject duplicate trust-manifest JSON fields. |
| `_unexpected_fields` | Find manifest fields outside the supported schema. |
| **`FieldDiff`** | Typed record including `field`, `kind`, `expected`, `committed`, `committed_present`. |
| `as_dict` | Serialize one expected-versus-committed manifest field difference. |
| **`ManifestComparison`** | How a committed copy relates to the expected manifest. |
| `status` | `ok`, `warn` (check set or formatting only) or `fail`. |
| `code` | The failure code, in the preflight's vocabulary. |
| `warnings` | Return nonblocking check-set/canonical-format warnings from the comparison. |
| `as_dict` | Serialize manifest identity equality, check equality, validity, and field differences. |
| **Module functions** | Definitions in the linked module. |
| `_absent` | Construct the missing/invalid-manifest comparison result. |
| `compare` | Compare a committed copy (raw file content, `None` when absent) field by field. |

### [src.integration.app_mode](../../../src/integration/app_mode.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`AppModeItem`** | One concern's verdict. |
| `code` | Return the first diagnostic code, if present. |
| `as_dict` | Serialize one App readiness item with expected, observed, and fix fields. |
| **`AppModeReport`** | Typed record including `items`, `expected`. |
| `blockers` | Every `fail` code, in item order: the preflight's blockers. |
| `warnings` | Every `warn` code that is not already a blocker. |
| `ready` | Report readiness when the evaluation has no blocking codes. |
| `item` | Find an evaluated item by its diagnostic id. |
| `as_dict` | Serialize aggregate readiness, blockers, warnings, and observations. |
| **Module functions** | Definitions in the linked module. |
| `required_protection` | The protection classification a project mode needs (spec §8.2); `None` is any. |
| `target_ruleset_name` | Name the default branch's train-only ruleset. |
| `target_ruleset` | The default branch's ruleset in train mode (spec §8.1), for this App. |
| `aq_command` | `aq integration VERB PROJECT` with the evaluation's own inputs. |
| `_describe` | Produce a bounded error description without raw GitHub diagnostics. |
| `decoded_content` | The bytes of a contents-API file response. |
| `read_file_at` | One file at an exact commit, through the App; `None` when it is absent there. |
| **`_DefaultBranch`** | The default branch's SHA, resolved once so every item reads the same tree. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `sha` | Cache the exact default-branch head or the error from its first read. |
| `_resolve` | Read the configured default branch's exact authenticated remote head. |
| **Module functions** | Definitions in the linked module. |
| `_unchecked` | An item that needs the App client, after the credential item failed. |
| `_credential` | Prove the configured installation can mint the required repository-scoped App token. |
| `_repository` | Check numeric repository identity, full name, and default branch against GitHub. |
| `_producer` | Require one numeric CI producer identity across both policy boundaries. |
| `_manifest` | The default-branch manifest, compared on identity; the check set only warns. |
| `_variables` | Both Actions variables, against the authority only (spec §6.3). |
| `expected_variables` | Render the expected attestation App and required-check-version Actions variables. |
| `check_protection` | The default branch's protection, classified for the App and judged for the mode. |
| `_protection_fix` | Render an actionable repair command for a protection classification failure. |
| `_audit_workflow` | A default-branch workflow that reads the attestation App id (spec §7.2). |
| `evaluate` | Every App-mode item for one repository, in `ITEM_IDS` order. |
| `_expected` | Assemble expected manifest bytes, digest, Actions variables, and target ruleset. |

### [src.integration.protection](../../../src/integration/protection.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`ProtectionRule`** | One effective rule, judged for the App's push. |
| `bypassed` | Report whether the App bypasses this effective protection rule. |
| `as_dict` | Serialize rule origin, requirements, bypass, and refusal evidence. |
| **`ProtectionReading`** | Typed record including `classification`, `rules`, `reason`. |
| `development_publisher_blocked` | Whether a rule the App cannot bypass refuses the publisher's push. |
| `as_dict` | Serialize effective protection classification, rules, and read failures. |
| **Module functions** | Definitions in the linked module. |
| `required_classification` | The classification a project mode needs (spec §8.2); `None` is any. |
| `judge` | The blocker and warning codes of *reading* under a project mode (spec §8.2). |
| `_policy_checks` | The root boundary's numeric producer and check names; only root candidates become main. |
| `_pinned` | Accept a numeric App pin only when it is a real integer. |
| `_status_checks` | `(refusal, requires_attestation)` for one set of required checks. |
| `_ruleset_rule` | Classify an effective ruleset rule's attestation requirement and App refusal. |
| `_enabled` | Test an explicit classic-protection enabled flag. |
| `_lists_app` | Check whether a protection actor list includes the exact numeric App. |
| `_classic_rules` | Classic branch protection as rules; `None` (a 404) is none. |
| `classify` | Classify the three reads for the App's promotion push (spec §8.3). |
| `_rule_label` | Name a ruleset or classic-protection rule in diagnostic text. |
| `_describe` | Render bounded typed protection-read errors without raw GitHub diagnostics. |
| `read_protection` | Read and classify the default branch's protection through *client*. |
| `_resolve` | Await an injected protection resolver result if needed. |
| `_app_client` | `(binding, client, identity)` for *repository*; `None` without a GitHub binding. |
| `bound_policy` | *project*'s hierarchical policy; `None` when it has none (development binds its own). |
| `enablement_reader` | The attestation service's `protection_reader`: the strict modes' blockers. |
| **`DevelopmentPublisherBlocked`** | Development refused: the App's unattested push would be refused (spec §8.2). |
| `__init__` | Configure the service’s collaborators and retained state. |
| `blocker` | Serialize the development protection refusal with repository identity. |
| **Module functions** | Definitions in the linked module. |
| `development_guard` | Refuse the development publisher while the App cannot push unattested. |

## Development publication and delivery truth

### [src.integration.development](../../../src/integration/development.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_publishable_object_task` | Experimental object candidates are artifact-only, including on replay. |
| `armed_for_branch_cleanup` | *evidence* with branch cleanup armed, for a row that just landed on main. |
| `operation_rows_on` | Latest event revisions of real publisher actions, independent of git truth. |
| `revise_operation_on` | Append a revision of operation *identity* on the caller's transaction. |
| `_manifest_members` | Filter a journal manifest to dictionary members carrying task ids. |
| `_dependency_cycles` | Return strongly connected components that actually contain a cycle. |
| `_carried_sources` | Every exact revision *repair_id* carries, through the repairs it replaced. |
| `row_repair_identity` | The repair carrying parked *row*. |
| `repair_chain` | Follow a parked batch through its repairs to the one still carrying it. |
| `describe_repair_chain` | One line naming each repair in *result*'s chain and its status. |
| `repair_statuses` | Every development repair's status in *project_ids*, live table over archive. |
| **`ResolvedTask`** | The publisher's view of a task, wherever the task currently lives. |
| `terminal` | Test whether a live/archive-resolved task has a terminal status. |
| **`DevelopmentPolicy`** | Typed record including `validation`, `commands`, `timeout_seconds`, `slot_wait_seconds`, `interval_seconds`. |
| `checked` | Validate required commands, target, and finite development validation settings. |
| **Module functions** | Definitions in the linked module. |
| `publisher_exclusion` | Hold *repository_id*'s development publisher lock, or raise `DevelopmentBusy`. |
| **`DevelopmentIntegration`** | Journal and publish owed source generations through bounded validation and fenced Git writes. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `on_task_completed` | Wake delivery without doing Git or validation in the completion path. |
| `exclusion` | Acquire the shared per-repository development publisher advisory lock. |
| `delivery_observer` | Git delivery truth for this service's readers, in its own isolated store. |
| `branch_holds` | `live_branch_references` with git proof for the tasks owning *branches*. |
| `run_git` | Run asynchronous Git in the publisher checkout and reject failed commands. |
| `_store_path` | Name the isolated publisher checkout by repository digest. |
| `merge_member` | Merge *commit* into the checkout's detached HEAD. |
| `_changed` | List changed paths between two commits with rename detection disabled. |
| `generated_paths` | The subset of *paths* the checkout's `.gitattributes` marks generated. |
| `run_git_input` | Run asynchronous Git with supplied stdin and reject failed commands. |
| `regenerate` | Run the policy's regeneration command in *store*, without a shell. |
| `store` | Create/fetch the private publisher checkout for the repository. |
| `remote` | Read an exact remote ref and reject an unknown observation. |
| `read_snapshot` | Yield a freshly fetched snapshot taken in a private repository. |
| `_operation_insert` | Append genuine operation intent/evidence to the existing event log. |
| `save` | Append a new development-operation journal event. |
| `change` | Append a revision of an existing development operation or reject a missing id. |
| `rows` | Latest operation revisions; no delivery receipts are read. |
| `_has_pending_work` | Check durable work before opening the authenticated Git transport. |
| `rebind_foreign_repositories` | Deliver this project's tasks that still name another project's repository. |
| `refresh_dependencies` | Repair projections from before delivery-aware readiness was installed. |
| `reconcile` | Classify interrupted writes from actual remote heads and candidate ancestry. |
| `validate` | Run the selected validation and classify it. |
| `_open_deferral` | Find the newest open infrastructure-validation deferral streak. |
| `_record_validation_deferral` | Journal one infrastructure outcome on the project's open streak row. |
| `_alert_validation_infrastructure` | Tell the project supervisor once per streak; the id makes it idempotent. |
| `_close_validation_deferrals` | A validation that reached a conclusion ends the deferral streak. |
| `_release_unverified_parks` | Release batches parked as validation failures that verified nothing. |
| `publish` | Journal and push an exact validated head with the observed target as its lease. |
| `adopt` | Record a fenced operator completion/equivalence decision in git. |
| `_completion_generations` | Each task's completion count and latest close, the generation adopt fences. |
| `_adoption_fence` | Bind operator adoption to task status, branch, claim epoch, and completion generation. |
| `_observe_adoption` | Name each task's current branch head and whether *head_sha* contains it. |
| `_record_adoption` | CAS task identities and record explicit Git equivalence/completion evidence. |
| `sweep` | Assemble, validate and publish one batch from a fresh target snapshot. |
| `_sweep` | Select delivery-owed sources, merge independent work, validate, park failures, and publish. |
| `recover_child` | Run a supervised sweep and prove the named child's delivery. |
| `_parked_repair_note` | Say which repair carries *task_id*'s parked source, or that none does. |
| `reconcile_parked` | Dispatch only failures still unresolved after the whole batch was assembled. |
| `_repair_id_for` | The repair id a new park of *manifest* on *target* records. |
| `_reclaim_repair` | Drop *repair_id*'s settlement on *target*: a live park needs it again. |
| `_repair_links` | The repairs filed for parked *row* and its successors, as they exist. |
| `_cancel_settled_park` | Cancel a park whose members this target does not owe, with its repairs. |
| `_retire_retargeted_parks` | Cancel this repository's parks on a target it no longer publishes to. |
| `_settle_not_owed` | Record pending work this target does not owe; return what was settled. |
| `settle_parked` | Settle or dismiss one parked publisher operation on a stated reason. |
| `_name_diagnostic` | Name a batch-scoped fault instead of raising it. |
| `_record_batch_diagnostic` | Persist at most one diagnostic per batch, and log only on change. |
| `resolve_task` | Return *task_id* from the live table, else from the archive, else `None`. |
| `_repair_identity` | Hash the exact parked manifest into its deterministic repair-task id. |
| `_merge_tree` | Return the merged tree and conflicted paths, without touching a checkout. |
| `_conflict_evidence` | Name what *source* conflicts with, each claim proven by its own merge. |
| `tick` | Sweep enabled development projects independently, then preserve stopped owners and clean branches. |
| `_note_project_fault` | One structured warning per project per state change. |
| `collect_delivered_branches` | Delete from origin the refs that confirmed main deliveries made obsolete. |
| `_plan_branch_cleanup` | Decide what *row*'s landing lets go of. |
| `_record_branch_cleanup` | Write one attempt's result onto *row*; log the refs it deleted. |
| `stale_branches` | Origin `aq/` branches the branch policy lets go of, and what holds the rest. |
| `configure` | Enter (or reconfigure) development mode for *project_id*. |
| `cancel_preserving` | Cancel an operation while retaining refs/workspaces and settling only proven stopped writers. |
| `preserve_stopped_owners` | Retain the old checkout intact and make a fresh workspace claim possible. |
| `ensure_repair` | File or reuse one bounded repair with provenance and delivery holds. |
| `_repair_evidence` | What the parked batch recorded, compact enough for task metadata. |
| `_conflict_attribution_text` | What a parked conflict was proven to conflict with; rows before this say nothing. |
| `_regeneration_text` | How a repair treats generated files, when the batch could rebuild them. |
| `_repair_failure_text` | The part of a repair description that names what failed. |

### [src.integration.development_validation](../../../src/integration/development_validation.py)

| Scoped name | One-line purpose |
| --- | --- |
| `classify` | Compatibility classifier for development validation's exit-5 deferrals. |
| `_detail` | Describe actual job outcome, run budget, slot wait, and validation exit. |
| `run_check` | Submit a finite preset and consume the queue's immutable result. |
| `conclude` | One conclusion for a validation: a real failure outranks an outage. |
| `failing_tests` | Every failing test an evidence record names, parsing older records. |
| `reclassify` | The conclusion a stored validation record deserves under these rules. |

### [src.integration.development_result_parser](../../../src/integration/development_result_parser.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_bounded` | Truncate retained UTF-8 diagnostics without splitting a code point. |
| **`_Failures`** | Bounded ids, with classification facts retained beyond the id cap. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `add` | Retain bounded unique failing-test reasons and infrastructure signatures. |
| `report` | Build a finite pytest result with integrity, summary, and failure evidence. |
| **`PytestOutputParser`** | Incremental UTF-8 parser; memory does not grow with output or line size. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `feed` | Incrementally decode bounded output chunks without blocking pipe draining. |
| `_text` | Retain bounded output and feed complete lines to the parser. |
| `_consume_line` | Extract failing ids, summary counts, no-tests signals, and infrastructure signatures. |
| `finish` | Flush the decoder and final line once, then return the immutable report. |
| **Module functions** | Definitions in the linked module. |
| `parse_pytest_output` | Compatibility entry point using the same bounded streaming parser. |
| `parse_junit` | Parse a selected JUnit artifact, without reading files or expanding entities. |
| `_killed_by` | Recognize configured termination signals in positive or negative process exit codes. |
| `classify_report` | Classify observed exit and integrity; parser text never implies success. |

### [src.integration.development_stalls](../../../src/integration/development_stalls.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`SweepObservation`** | What one sweep saw, for fingerprinting the candidates it skipped. |
| `source_of` | Resolve a candidate branch's source SHA from the fetched sweep snapshot. |
| **Module functions** | Definitions in the linked module. |
| `fingerprint` | Hash a candidate's skip evidence into a stable attempt fingerprint. |
| `_attempt_id` | Hash task, evidence fingerprint, and start time into the publisher attempt id. |
| `stall_message_id` | One message per set of attempts that stalled together; restart-safe. |
| `is_stalled` | Whether *record* describes a stalled attempt. |
| **`PublisherStalls`** | Record skip observations and end identical runs of them as stalls. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `clear` | Nothing is left to evaluate, so no skip record is current. |
| `observe` | Persist this sweep's skip observations; return attempts that just stalled. |
| `advance` | The record after one more skip of *task_id*, given its *old* record. |
| `_records` | Read persisted per-task publisher stall observations. |
| `_generations` | Latest completion record id per task: its completion generation. |
| `_live_repairs` | Map each skipped candidate waiting on a live repair to that repair. |
| `_message` | Build the idempotent supervisor message for candidates whose attempts just stalled. |
| `_write` | Apply one sweep's observations, and its notification, atomically. |

### [src.integration.development_settlement](../../../src/integration/development_settlement.py)

| Scoped name | One-line purpose |
| --- | --- |
| `publication_target` | Whether a journal row's *ref* is a publication target. |
| `_configurations` | Select and order this repository's explicit publisher configuration events. |
| `_published_before` | The target of the newest publisher row older than *created_at*, if any. |
| `previous_target` | The target `aq integration develop` is moving away from, or `None`. |
| `retarget_of` | The retarget onto *target_ref*, or `None` when there was none. |
| `repair_target_of` | The target a development repair was filed to publish to, if known. |
| `settlement_record` | One `SETTLEMENT_KEY` value: which target, which generation, and why. |
| `write_settlements_on` | Record *records* (task id -> settlement) on the caller's transaction. |
| `notify_settlements_on` | Tell `supervisor-<project>` what was settled automatically, once. |

### [src.integration.delivery_truth](../../../src/integration/delivery_truth.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`DeliveryRequest`** | Immutable current task/completion inputs, including archived identities. |
| `settles` | Whether the recorded settlement covers this target and generation. |
| `from_task` | Construct the task's current repository, target, branch, and completion-generation request. |
| **`DeliveryEvidence`** | Typed record including `request`, `state`, `target_oid`, `source_oid`, `reason`. |
| `satisfied` | Accept contained, no-artifact, or explicitly settled delivery obligations. |
| **`DeliverySnapshot`** | One fetched repository/target with a cache confined to this request. |
| `for_request` | The same fetched observation without another request's cached answers. |
| `matches` | Caller must reload the same task set and relevant graph inputs. |
| `is_fresh` | Require the snapshot's fetched target to still equal the exact remote ref. |
| `evaluate_many` | Evaluate independently: one broken identity cannot poison peers. |
| `evaluate` | Prove the current artifact from Git completion provenance, ancestry, or explicit target settlement. |
| `unlabelled_inventory` | Evaluated generations with an artifact but no exact source in git. |
| **Module functions** | Definitions in the linked module. |
| `_run` | Run asynchronous Git for delivery proof and reject failed observations. |
| `load_delivery_requests` | Batch read current completions and live/archive identities without locks. |
| `settlement_fields` | The `DeliveryRequest` fields of one stored settlement. |
| `delivery_snapshot` | Fetch once and pin all batch observations to its exact target OID. |

### [src.integration.delivery_observer](../../../src/integration/delivery_observer.py)

| Scoped name | One-line purpose |
| --- | --- |
| `development_delivery_scope` | `EXISTS`: *task*'s completed work must reach its project's target. |
| `delivery_sensitive_ids` | The COMPLETED tasks among *task_ids* whose delivery git must prove. |
| `gating_delivery_ids` | Completed development work that open work waits on, newest first. |
| `delivery_targets` | Each task's project target, for live and archived identities alike. |
| `_unknown` | Construct explicitly unknown delivery evidence for an unproved snapshot. |
| **`DeliveryView`** | Evidence for one request, and the checks a guarded writer repeats. |
| `get` | Look up one task's observed delivery evidence. |
| `satisfied` | Report whether the task's observed current delivery obligation is satisfied. |
| `fresh` | Every inspected target is still at the OID this view evaluated against. |
| `verified_on` | Evidence whose identity and target still hold on *conn*. |
| **Module functions** | Definitions in the linked module. |
| `_fetch_lock` | One in-process lock per observer store, so fetches never race each other. |
| **`DeliveryObserver`** | Fetch and evaluate delivery truth for arbitrary tasks, outside transactions. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `store_path` | Name the isolated Git delivery-observer checkout by repository digest. |
| `_store` | Name the retained bare integration repository by canonical repository digest. |
| `_snapshot` | A fetched snapshot, or an unknown one: a failure is never an empty answer. |
| `snapshot` | Fetch one isolated, request-scoped snapshot for an external reader. |
| `_evaluate` | Load current completion requests and evaluate them against a fresh target snapshot. |
| `observe` | Evaluate each task's current completion against its project's target. |

### [src.integration.admission](../../../src/integration/admission.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_inputs` | Read graph inputs and immutable completion identities on one connection. |
| **`AdmissionSnapshot`** | Typed record including `db`, `candidate_ids`, `allowed`, `inputs`, `snapshots`. |
| `is_fresh` | Recheck the delivery snapshots required by a candidate task. |
| `matches` | Fence graph/generation/config under the mutation's transaction. |
| **Module functions** | Definitions in the linked module. |
| `observe_admission` | One batch for readiness, pool demand, explanation and actual claims. |
| `structural_candidates` | Keyset page past withheld candidates in the existing fairness order. |

### [src.integration.provenance](../../../src/integration/provenance.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`ProvenanceMigrationRequired`** | Legacy repair evidence needs an authorized operator migration. |
| `__init__` | Configure the service’s collaborators and retained state. |
| **Module functions** | Definitions in the linked module. |
| `task_message` | Append task identity without amending history or changing hook configuration. |
| **`CompletionIdentity`** | Typed record including `project_id`, `repository_id`, `task_id`, `generation`. |
| `__post_init__` | Validate nonempty immutable completion identity fields and reject control characters. |
| `branch` | Name the immutable completion-provenance ref by subject and generation digests. |
| **`CompletedSource`** | Typed record including `identity`, `source_oid`. |
| `__post_init__` | Require the completed source to carry a valid exact commit object id. |
| **Module functions** | Definitions in the linked module. |
| `_json` | Serialize Git provenance records with deterministic field order and separators. |
| `_completion_record` | Build the versioned completion record with source, artifact, and claim identity. |
| **`GitProvenance`** | Read and publish immutable completion/replacement provenance in Git. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `run` | Run strict asynchronous Git in the provenance repository. |
| `exact` | Resolve and require a valid exact Git object id. |
| `ancestor` | Prove ancestry while treating failed Git observations as errors. |
| `_validate` | Validate record schema and Git parent/source/replacement semantics against its commit. |
| `_changed_source` | Require an explicit replacement to contain the original completed source history. |
| `_read` | Read and validate the exact immutable provenance ref's canonical record. |
| `read_completion` | Read only the requested current completion-generation ref, never historical task trailers. |
| `_stage` | The validated local metadata commit for *record*; nothing is published. |
| `_write` | Stage and publish an immutable provenance ref using an exact absent/existing lease. |
| `write_completion` | Publish the exact source's immutable completion record. |
| `write_completions` | `write_completion` for many generations in one remote transfer. |
| `write_replacement` | Called only after operator authorization or exact repair-contract fencing. |
| `contained` | Proof primitive for the shared evaluator; failures propagate as unknown. |
| **Module functions** | Definitions in the linked module. |
| `record_worker_completion` | Verify and publish completion evidence before the task can become terminal. |
| `legacy_repair_source` | Exact source of a repair-contract original closed before provenance existed. |
| `filing_operation_on` | The publisher attempt a repair's evidence names, as first recorded. |

### [src.integration.provenance_migration](../../../src/integration/provenance_migration.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`ProvenanceMigration`** | Inventory and rebind legacy completion generations to immutable source refs. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `run` | Inventory one page of completion generations, or only a held task's sources. |
| `_bind` | Bind one batch of generations; an apply publishes its new refs in one transfer. |
| `_operations` | Outstanding legacy actions the retirement retained as operation events. |
| `_held_task_rows` | Current source generations a held task's close needs, keyed by contract. |
| `_history` | Keyset-page the retained legacy journal, keeping rows naming *task_ids*. |
| `_source` | Resolve one legacy generation's source from exact close and explicit publisher bindings. |
| `_repairs` | Validate and record legacy replacement chains without inventing equivalence. |
| **Module functions** | Definitions in the linked module. |
| `_journal_row` | A retained legacy provenance payload in the retired journal row's shape. |
| `_binding_key` | Key a legacy binding by task, completion generation, and exact source object. |
| `_names` | Whether a legacy delivery row locates or supersedes any of *task_ids*. |

### [src.integration.publishable_artifact](../../../src/integration/publishable_artifact.py)

| Scoped name | One-line purpose |
| --- | --- |
| `legacy_artifact` | `EXISTS`: *task*'s current generation has a legacy artifact of unknown provenance. |
| `has_publishable_artifact` | A branch identity can name a publishable source. |

### [src.integration.delivery_path](../../../src/integration/delivery_path.py)

| Scoped name | One-line purpose |
| --- | --- |
| `lacks_pull_request_host` | True when *repository_url* is set and is not a github.com repository. |
| `local_repository_path` | The filesystem path a local repository URL names, else `None`. |
| `task_repository_url` | The repository a task delivers to: its bound repository row, else the project's. |
| `effective_integration_mode` | `(mode, source)` for *task*: the single authority for the policy chain. |
| `delivery_path_problems` | Why *project* has no working delivery path; empty when it has one. |

## Cleanup, preservation, and removal

### [src.integration.cleanup](../../../src/integration/cleanup.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`IntegrationCleanupService`** | Materialize immutable cleanup work; later calls execute one claimed item. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `advance` | Page due cleanup aggregates and reconcile their terminal state. |
| `handle_item` | Dispatch a durable cleanup row to its exact kind and identity. |
| `execute` | Claim one cleanup item, perform its guarded action, and CAS its result/backoff. |
| `_perform` | Route cleanup to PR closure, remote ref, local ref, or retained worktree handling. |
| `_cleanup_pr` | Comment and close the exact delivered PR with idempotent write evidence. |
| `_repair_commit_summary` | Render the repair commits recorded in the delivered batch's cleanup evidence. |
| `_mark_irreversible_prewrite` | Freeze one claim before an ambiguous external write; marked writes never transfer. |
| `_github_client` | Resolve an App client bound to the cleanup repository. |
| `_cleanup_remote_ref` | Delete only an unowned exact delivered remote ref, excluding the default branch. |
| `_cleanup_local_ref` | Remove an exact delivered local ref only with matching ownership and worktree proof. |
| `_cleanup_worktree` | Remove a recorded retained worktree only after exact path, head, and ownership checks. |
| `_finalize` | CAS the cleanup claim's outcome, retries, and aggregate projection. |
| `reconcile_aggregate` | Reconcile cleanup completion and its detached collector reservation. |
| `_project_aggregate_on` | Mark aggregate cleanup complete and release only its recorded detached collector fence. |
| `_cleanup_policy` | Read bounded cleanup attempts and retry parameters from the frozen batch policy. |
| `_short_head` | Normalize a refs/heads name and reject unsupported ref spellings. |
| `_execution_result` | Serialize the cleanup outcome, item identity, attempts, and error. |
| `materialize` | Freeze cleanup items from the committed root intent and delivered member identities. |
| `_descendant_ref_items` | Only delete descendant refs with a delivered, exact source head. |
| `_items` | Build PR and branch cleanup items from the committed batch members. |
| `_worktree_items` | Build retained-worktree cleanup items with exact ownership/head evidence. |
| `_pr_number` | Validate and parse a repository-bound pull-request URL. |
| `_same_identity` | Compare immutable cleanup fields while excluding mutable retry state. |
| `retained_store` | Name the durable retained bare repository for cleanup and recovery. |

### [src.integration.release](../../../src/integration/release.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`IntegrationReleaseService`** | Release one shipped train without coupling release to cleanup progress. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `release` | Release a terminal root batch's scheduling capacity using exact delivery evidence. |
| `_release_locked` | Lock project/batch/schedule, prove terminal shipping or safe ending, and release its request/lease. |
| `_release_ended_on` | Free the request of a batch that stopped without shipping. |
| `_release_stale_request_on` | Classify and clear only the outstanding request proven stale in this release transaction. |
| `_canonical_replay` | Return the persisted release outcome and any still-current catch-up request. |
| `_one_for_update` | Lock and read one release authority row as a dictionary. |
| `_all_for_update` | Lock and read ordered release authority rows as dictionaries. |
| `_has_unresolved_on` | Detect unresolved batch ref writes, intents, or publication preventing release. |
| `_complete_shipping` | Require the exact promoted revision, committed intent, complete receipts, and final main head. |
| `_replay_catchup` | Return a current outstanding request distinct from the released one. |
| `_result` | Serialize released batch, project, operation, request, and catch-up identity. |
| `_persisted_result` | Decode and validate the immutable release result on replay. |

### [src.integration.branch_discard](../../../src/integration/branch_discard.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_month` | Choose the UTC month for retained deletion backups and logs. |
| `_bundle` | Write *heads* to a new verified bundle; raise rather than return an unproved one. |
| `_record_deletions` | Append one line per branch to the month's deletion log, durably. |
| **`BranchDiscardService`** | Drain `task_branch_origins` rows marked for branch discard. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `drain_due` | Advance every discard whose backoff has elapsed, oldest request first. |
| `advance` | Attempt one discard, claiming it by its attempt count. |
| `_perform` | Prove the exact obsolete branch, preserve its tip, and delete through the App. |
| `_live_owner` | Find any unreleased owner preventing branch deletion. |
| `_binding` | Resolve the repository's immutable GitHub binding. |
| `_github_client` | Require a configured App client bound to that exact repository. |
| `_backup_before_delete` | Bundle a doomed head and record the row *before* the ref is deleted. |
| `backup_dir` | Return the durable branch-deletion backup directory. |
| `_run_git` | Run asynchronous Git and reject a nonzero result. |
| `_ensure_store` | Atomically initialize the retained bare repository using a private staging directory. |
| `_finalize` | CAS the discard attempt's outcome and next retry time. |
| `_backoff` | Compute capped exponential discard retry delay. |
| `_branch` | Read the origin's recorded branch name. |
| `_result` | Serialize a discard result with origin, task, branch, attempt, and error. |
| `retained_store` | Name the durable retained bare repository for cleanup and recovery. |

### [src.integration.delivery_branches](../../../src/integration/delivery_branches.py)

| Scoped name | One-line purpose |
| --- | --- |
| `branch_of` | `refs/heads/aq/x` or `aq/x` -> `aq/x`; anything empty -> `None`. |
| `_manifest` | Decode a publisher manifest and retain dictionary members with task ids. |
| `completed_branch_tasks` | COMPLETED tasks in development delivery scope that own one of *branches*. |
| `live_branch_references` | Every branch something still needs, mapped to the first reason found. |
| `_branches_of` | `(task_id, branch_name)` for *task_ids*, live table first, then archive. |
| `remote_heads` | Branch -> head for every remote-tracking ref of `origin` in *store*. |
| `deletable` | The one rule no caller can override: `aq/` only, never a protected name. |
| `delete_branches` | Back up, record, then delete each `branch -> {"head", "reason"}` from `origin`. |
| `_month` | Choose the UTC month for branch deletion backup records. |
| `_bundle` | Write *heads* to a new verified bundle; raise rather than return an unproved one. |
| `_record_deletions` | Append one line per branch to the month's deletion log, durably. |
| `released_integration_refs` | `aq/integration/*` branches rule (a) lets go of, with why. |
| `expired_task_branches` | Branches of FAILED or abandoned tasks terminal for *keep_seconds* (rule b). |
| `find_stale_branches` | Classify every branch of `origin` under the branch policy. |
| `_merges_are_clean` | Whether every merge in `main_head..head` is Git's own merge of its parents. |

### [src.integration.removal_guard](../../../src/integration/removal_guard.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_error` | Construct the public hierarchy removal refusal without a module import cycle. |
| `_sealed_member_on` | Return the first active batch member in the subtree or its ancestors. |
| `_is_delegate_on` | Detect whether an operation/stage still records the task as a generated delegate. |
| `_cleanup_blockers_on` | Return only preserved delegate resources, not harmless stale locks. |
| `_development_undelivered_on` | The COMPLETED tasks in *ids* whose work git has not proven on the target. |
| `_hierarchy_undelivered_on` | Return default branch and the root's undelivered holder(s), if any. |
| `undelivered_removal_holders` | Return the delivery holders an archive would otherwise refuse. |
| `assert_integration_permits_removal` | Raise the first ordered integration refusal for a removal. |

### [src.integration.obsolete_close](../../../src/integration/obsolete_close.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`ObsoleteCloseRefused`** | The task cannot be closed as obsolete; `code` says why. |
| `__init__` | Configure the service’s collaborators and retained state. |
| **Module functions** | Definitions in the linked module. |
| `obsolete_owner_release_for` | The daemon's owner-release callable, or `None` when it cannot be built. |
| **`ObsoleteClose`** | Close superseded work and release its branch owners and batch membership. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `close` | Close *task_id* as obsolete, then run its cleanup. |
| `_refuse_unsafe` | Reject obsolete closure with live descendants/writers or integration history requiring its own decision. |
| `_marker` | Decode the task's explicit obsolete marker without assuming malformed metadata is valid. |
| `cleanup` | Release what *task_id* still holds; record and return what remains. |
| `retry_pending` | Re-run `cleanup` for every obsolete task whose cleanup is pending. |
| `_owner_rows` | Read unreleased branch owners still attached to the obsolete task identity. |
| `_unsettled_batches` | Publisher operations still in flight whose manifest lists *task_id*. |
| `_open_repair_naming` | Find active development repairs whose frozen evidence still names the task. |
| `_active_train_batches` | Find nonterminal train batches still containing the task. |
| `_cancel_parked` | Cancel one parked batch under the publisher's lock; `None` on success. |
| `_record_cleanup` | Persist obsolete-close cleanup decisions and their audit event. |
| **Module functions** | Definitions in the linked module. |
| `_pending_owner` | Serialize an unreleased owner blocking obsolete cleanup. |
| `_pending_batch` | Serialize an unresolved publisher operation blocking obsolete cleanup. |

### [src.integration.legacy_deliveries](../../../src/integration/legacy_deliveries.py)

| Scoped name | One-line purpose |
| --- | --- |
| `legacy_delivered_children_on` | The terminal children of a terminal *parent* status accepts as delivered. |
| **`LegacyDeliveryAdoption`** | Record legacy delivery rows for the children status flags without a collection. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `run` | Prove what can be proven; apply the named decisions to the rest. |
| `_flagged_children` | Children status reports as `missing_receipt` with no parent collection. |
| `_deliveries_by_task` | Sources retired development journal rows located, by listed task. |
| `_completion_commits` | The last commit each task's latest completion reported, by task. |
| `_prove` | Adopt *child* on the first proof that reaches *target*; else say why not. |
| `_on_target` | Prove a legacy SHA is contained by the observed delivery target. |
| `_git` | Run asynchronous Git in the retained legacy-adoption store. |
| `_commit` | *sha* resolved to one commit present in *store*, or `None`. |
| `_tree` | Require and read the exact legacy commit's tree identity. |
| `_merge` | Merge *sha* into *target* without touching a ref; `None` if unexaminable. |
| `_undelivered` | What merging *merge*'s commit into the default branch would still change. |
| `_record` | CAS proven detached legacy source identities and record explicit adoption evidence. |
| **Module functions** | Definitions in the linked module. |
| `_result` | Serialize legacy adoption outcome, project, dry-run, and per-source evidence. |
| `legacy_delivery_adoption_for` | The command handler's adoption service, or `None` without a DB and Git. |

### [src.integration.legacy_repositories](../../../src/integration/legacy_repositories.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`LegacyRepositoryBinding`** | Bind historical tasks to the designated repository with delivery proof. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `_observe` | Git evidence for the project's unbound completed tasks, before any lock. |
| `run` | Bind detached legacy tasks to the designated repository only with current delivery proof and audit reason. |

## Operator recovery, rebind, and rollout controls

### [src.integration.controls](../../../src/integration/controls.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_blocker` | Build a control blocker with code, detail, and ref. |
| `_sorted_blockers` | Deduplicate and deterministically order control blockers. |
| `_blocker_digest` | Hash canonical blocker JSON for rollout/history-waiver CAS. |
| **`IntegrationControlService`** | Functional preflight plus atomic rollout and recovery controls. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `_recovery` | Construct the operation recovery facade with the configured legacy observer. |
| `resume` | Forward operation resume to IntegrationRecoveryControls. |
| `abort` | Forward explicit operation abortion with its audit reason. |
| `clear_stale_request` | Report whether the outstanding sweep request can still end; free it if not. |
| `retry_cleanup` | Requeue retryable cleanup, or materialize a promoted batch's missing cleanup. |
| `release_delegates` | Release ended-operation delegates or archive proven obsolete delegates. |
| `has_active_work` | Read whether durable train work prevents a mode transition. |
| `preflight` | Read functional readiness without persisting observations. |
| `_preflight_with_warnings` | The preflight projection and its non-blocking warnings. |
| `status` | Return status plus live functional wiring blockers. |
| `_functional_preflight_on` | Validate rollout repository, policy, routes, history, and current control generation. |
| `enable` | CAS an integration mode transition, enforcing readiness, history waivers, and safe draining. |
| `configure` | Configure rollout inputs only while fully disabled and drained. |
| `reconcile_unmaterialized_tasks` | Atomically bind pre-rollout, unclaimed work to the enabled route. |
| `flush` | Request an immediate train sweep only for an enabled, nondraining, ready project. |
| `eject` | Eject before construction or rebuild a safely detached repair candidate. |
| `_observed_ejection_rejections_on` | Prove that a refused private push is retained evidence, not a live write. |
| `waive_history` | Record an operator waiver for the exact current preflight blocker digest. |
| `reconcile_drains` | Finish requested drains after all frozen work reaches a safe terminal state. |
| `_complete_drain` | Finish a requested mode transition only when every durable writer/work item is settled. |
| `_configure_schedule_on` | Create or reset schedule parameters for the selected integration mode. |
| `_legacy_policy_on` | Read the legacy project controls preserved across integration-mode transitions. |
| `_active_work_queries` | Define the batches, operations, owners, leases, intents, and cleanup that block draining. |
| `drain_blockers_on` | Expose the same durable items the mode-transition guard counts. |
| `_has_active_work_on` | Test the shared active-work queries inside a transition transaction. |
| `_github_origin` | Validate a canonical HTTPS GitHub origin without embedded credentials. |

### [src.integration.recovery_controls](../../../src/integration/recovery_controls.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`IntegrationRecoveryControls`** | Resume, abort, and retry only when no external mutation is ambiguous. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `resume` | Rearm a human-required operation only after proving safe delegate, collection, and write state. |
| `_recover_stopped_delegate_claim` | Recover the exact delegate's stopped pool claim before resuming its operation. |
| `_has_operator_resume_evidence` | Identify a stage whose deadline event records explicit operator resume. |
| `_restore_parent_collection_on` | Restore only the terminally blocked parent for this exact episode. |
| `_restore_completed_delegate_on` | Make an exact, safely released repair delegate dispatchable again. |
| `abort` | Cancel a nonterminal operation, preserve work, settle safe delegates, and release its ended request. |
| `release_delegates` | Settle the delegates of one operation that has already ended. |
| `retry_cleanup` | Requeue existing safe identities without changing any irreversible marker. |
| `_locked_operation_on` | Lock one durable repair operation. |
| `_locked_stage_on` | Lock the operation's exact active repair stage. |
| `_project_id_on` | Resolve operation scope from its batch or parent task identity. |
| `_safe_live_resolution_resume_on` | Prove the one never-started resolution push that may be re-armed. |
| `_safe_legacy_resolution_resume_on` | Observe legacy state without mutating it; authorization is deliberately delayed. |
| `_ambiguous_writes_on` | Collect pending writes, reservations, handoffs, and live writers that forbid recovery. |
| `_event_on` | Enqueue an idempotent audited integration-control event. |
| `_state_result` | Serialize a recovery outcome with operation, project, and current state. |
| `_ambiguous_result` | Explain the exact unresolved write evidence blocking an operation control. |

### [src.integration.repair_rebind](../../../src/integration/repair_rebind.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`RepairRebind`** | Reserve a proved live repair candidate under the current intent. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `run` | Prove a live delegate's candidate and reserve it under the current intent. |

### [src.integration.detached_repair_rebind](../../../src/integration/detached_repair_rebind.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`DetachedRepairRebind`** | Rebind a stopped parent repair from a proved unpublished descendant. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `run` | Prove a detached stage frozen on an unpublished head, then rebind it. |
| `_proof_on` | Prove the exact stopped parent delegate, current conflict, owner, receipts, and unchanged stage budget. |
| `_git_proof` | Prove the frozen head is an unpublished descendant of the published head. |
| `_rebind_on` | CAS the detached delegate's subject/trigger/dossier to the current conflict without extending its deadline. |
| `_identity` | Extract the immutable stage and delegate fields compared between preview and apply. |
| `_blocked` | Serialize a detached-rebind refusal with its evidence. |
| `_public` | Remove internal authority rows from the public detached-rebind proof. |

### [src.integration.preserved_repair](../../../src/integration/preserved_repair.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`PreservedRepairRecovery`** | Promote an audited preserved repair after lineage, stop, and current-scope proof. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `run` | Run preserved parent-repair recovery and convert typed refusals into operator guidance. |
| `_run` | Prove preserved lineage, reserve a fresh collector fence, journal/push the exact repair, and finalize. |
| `_proof_on` | Lock and validate preservation audit, parent stage/intent, detached owner, holds, and pending writes. |
| `_git_proof` | Prove the exact preserved resolution range/tree and freshly observed remote target. |
| `_record_on` | Record the preserved-repair recovery receipt and original authoring identity. |
| `_public` | Serialize the exact stage, intent, candidate, preservation record, and operator apply command. |

### [src.integration.identity_rebind](../../../src/integration/identity_rebind.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`ReusedIdentityRebind`** | Prove and rebind one task's inherited integration identity. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `run` | Report what the identity proves; rebind it when everything is settled. |
| `_prove` | Per inherited origin: every predecessor commit against the default tip. |
| `_apply` | Retire the origins and drop the checkpoint, re-proved under the lock. |
| `_comment` | Append the identity-rebind audit explanation to the affected task. |
| **Module functions** | Definitions in the linked module. |
| `_read` | The task, its inherited origins, checkpoint and the origins' owners. |
| `_blockers` | Every database fact that forbids rebinding *identity*, with its cause. |
| `_history` | Integration history that depends on the task's identity. |
| `_recorded_commits` | The predecessor commits the database recorded for *origin*, sha -> sources. |
| `_settle` | Apply the named discards and describe each origin for the report. |
| `_evidence` | Serialize predecessor origins/checkpoint, task identity, owners, proofs, and exact discards. |
| `_snapshot` | What the proof relied on, compared again under the lock. |
| `_blocker_line` | Render one typed identity-rebind refusal. |
| `_result` | Serialize the identity-rebind outcome with dry-run and evidence fields. |
| `reused_identity_rebind_for` | The command handler's rebind service, or `None` without a DB and Git. |

### [src.integration.manual_delivery](../../../src/integration/manual_delivery.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_refused` | Return a typed unsuccessful manual-delivery result. |
| **`ManualDelivery`** | Merge one BLOCKED task's pushed branch into its default branch. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `deliver` | Validate a passed BLOCKED task and manually deliver its exact pushed head to a non-PR target. |
| `_git` | Run asynchronous Git in the private manual-delivery checkout. |
| `_rev` | Resolve a local ref to an object id, returning absent on failure. |
| `_deliver_in` | Build a provenance-bearing merge and push it with the observed default-head lease. |

### [src.integration.pr_delivery](../../../src/integration/pr_delivery.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`DeliveredPullRequestClosure`** | Close one open PR of the designated repository once Git proves its work landed. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `run` | Preview or close a PR only after proving its exact current source already delivered. |
| `_observe` | Read the PR and prove its delivery; the GitHub client serves the apply path. |
| `_prove` | The first proof that reaches *target*, else what merging *head* would change. |
| `_git` | Run asynchronous Git under the retained repository transaction. |
| `_tracking_tasks` | Find task rows still linked to the exact pull-request URL. |
| **Module functions** | Definitions in the linked module. |
| `_all_marked` | Every PR commit is listed, and every one has a patch-identical twin. |
| `_marker` | Name the idempotent delivered-PR closure comment marker by PR number and head. |
| `_comment` | The public proof; the operator's audit reason stays in the event. |

### [src.integration.live_operations](../../../src/integration/live_operations.py)

| Scoped name | One-line purpose |
| --- | --- |
| `live_operations_on` | Return active repair operations whose parent or batch belongs to a project. |
| `cancel_preserving_command` | Return the safe operator command for ending a legacy operation. |
| `describe_live_operation` | Render an operation with its target and safe terminal command. |

### [src.integration.status](../../../src/integration/status.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_blocker` | Build a status blocker with optional structured evidence. |
| `_sorted_blockers` | Deduplicate and deterministically order status blockers. |
| **`IntegrationStatusService`** | Project/task integration projections with no provider I/O or writes. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `_observe` | A git view of *candidates*, prepared before any database snapshot. |
| `_delivery_candidates` | The completed tasks a projection will ask git about (no locks). |
| `_completed_children` | Select completed children relevant to a parent's delivery projection. |
| `_development_delivery_on` | What the scheduler and settlement wait for, as git answers it now. |
| `_consistent_snapshot` | Open a repeatable-read transaction for one coherent status projection. |
| `control_status` | Read durable control state without delivery or readiness observations. |
| `status` | Return one complete project projection from one database snapshot. |
| `task_blockers` | Return integration blockers after resolving task/project server-side. |
| `_task_blockers_on` | Project task-specific repair, checkpoint, parent-readiness, and delivery blockers. |
| `_own_delivery_blockers` | Whether this completed task's current work is on its target, per git. |
| `_one` | Read one query result as a dictionary. |
| `_all` | Read all query results as dictionaries. |
| `_repair_projection` | Project an operation, its stages, delegate, and deadline evidence. |
| `_batch_projection` | Serialize the batch's lifecycle, revision, manifest, base, and delivery identity. |
| `_project_blockers` | Collect project lease, cleanup, candidate, and repair blockers. |
| `_repair_blockers` | Describe repair-stage state, deadlines, missing delegates, and human-required conditions. |

### [src.integration.train_onboarding](../../../src/integration/train_onboarding.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`WorkflowReport`** | Typed record including `path`, `name`, `push_on_train_refs`, `pull_request`, `push`. |
| `required_names` | Return check names of statically required workflow jobs. |
| `is_ci_candidate` | CI that could gate the train once its push trigger names the train's refs. |
| **Module functions** | Definitions in the linked module. |
| `_triggers` | Normalize workflow trigger syntax including YAML's boolean interpretation of `on`. |
| `_filter_regex` | GitHub's branch filter glob: `**` crosses `/`, `*` does not. |
| `branch_filter_matches` | Whether a `push` trigger's branch filters run it for `branch`. |
| `_push_covers_train_refs` | Determine whether an unfiltered push trigger covers candidate train refs. |
| **`_IfParser`** | Parse restricted workflow condition expressions for onboarding. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `_peek` | Inspect the next restricted workflow-condition token. |
| `_take` | Consume the next condition token. |
| `parse` | Parse the complete supported condition and reject leftover tokens. |
| `_or` | Evaluate restricted disjunction with three-valued unknown handling. |
| `_and` | Evaluate restricted conjunction with three-valued unknown handling. |
| `_not` | Evaluate restricted negation while preserving unknown conditions. |
| `_comparison` | Evaluate supported event/ref/string comparisons without assuming unknown expressions. |
| `_atom` | Parse literals, event/ref identifiers, grouped expressions, and supported status functions. |
| **Module functions** | Definitions in the linked module. |
| `job_runs_on_push` | Whether a job `if:` runs for a push of a train ref; `None` when undecidable. |
| `_matrix_combinations` | Expand literal matrix axes/include rows or report unprovable expression-based names. |
| `_lookup` | Resolve a dotted matrix value from one expanded combination. |
| `_render` | Render a matrix scalar with Actions-compatible boolean/integer spelling. |
| `_substitute_matrix` | Substitute literal matrix values into a workflow check name. |
| `job_check_names` | The check-run names GitHub gives `job`, or a reason they cannot be known. |
| `_empty_report` | Return an uninspectable/empty workflow report with its warning reason. |
| `_needs` | Normalize a job's dependency names to a list. |
| `_permissions_problem` | What a `permissions` block asks beyond the audit's read-only token. |
| `_call_refusal` | Why the `main` push audit must not call this workflow; `None` when it may. |
| `analyze_workflow` | Read one workflow file: its triggers and the check runs each job produces. |
| `github_full_name` | `OWNER/REPO` for a github.com URL, else `None`. |
| `_is_local` | Recognize a local repository URL through the shared repository resolver. |
| `check_set_version` | A version that changes exactly when the required names change. |
| `_check_set` | The union of required names, and a problem for each name two jobs share. |
| `classify` | Decide the project's train shape from its repository URL and workflow files. |
| `bundle_route` | The frozen route identity of one reviewed bundle, exactly as an import stores it. |
| `select_routes` | `(parent, root)` routes: the project's own reviewed pair, or the shared pair. |
| `producer_for` | The policy producer: GitHub Actions' numeric App id in every credential mode. |
| `build_policy` | The project's hierarchical integration policy, validated by its model. |
| `build_trust_manifest` | `.github/agent-queue-integration.json` for App credential mode. |
| `detect_stack` | `python`, `pnpm`, `npm` or `unknown` from top-level file names. |
| `_package_scripts` | Decode a package manifest's scripts without assuming malformed JSON is usable. |
| `_python_install` | Select a Python dependency-install command from available project files and extras. |
| `suggested_commands` | Install-and-test commands for the detected stack, in order. |
| `ci_workflow_template` | A starting `.github/workflows/ci.yml` whose single `Tests` job gates the train. |
| `trigger_fix` | The push trigger the workflow needs, keeping what it runs on today. |
| **`AuditFallback`** | What `main-attestation.yml` runs for a push to the default branch it cannot trust. |
| `fails` | Whether a failing "unattested push" job stands in for CI it cannot re-run. |
| `as_dict` | Serialize fallback reusable-workflow calls and workflows that could not be called. |
| **Module functions** | Definitions in the linked module. |
| `audit_fallback` | The audit's fallback: the project's gating CI workflows that can be called safely. |
| `hosted_verifier_source` | `src/integration/hosted_attestation.py`, the text the audit embeds. |
| `_embeddable` | `source` as the body of a YAML literal block inside a quoted heredoc, unchanged. |
| `_call_job_ids` | Generate unique valid job ids for fallback workflow calls. |
| `audit_workflow_template` | `.github/workflows/main-attestation.yml` for one project. |
| `embedded_verifier` | The verifier a rendered audit workflow embeds, as the runner would write it. |
| **`OnboardingPlan`** | Typed record including `project_id`, `shape`, `path`, `required_checks`, `check_version`. |
| `as_dict` | Serialize the complete onboarding plan, generated files, warnings, and ordered steps. |
| **Module functions** | Definitions in the linked module. |
| `_q` | Quote one command argument for the rendered operator runbook. |
| **`_AppAnchors`** | App credential mode's repository-side trust anchors, as plan steps (spec §11). |
| `_aq` | Render an App setup command with the plan's policy/repository arguments. |
| `_audit_note` | Explain which generated attestation audit workflow must land on default. |
| `land_step` | Render the runbook step to land the inert trust manifest and audit workflow. |
| `variables_step` | Render repository-admin commands to set exact attestation Actions variables. |
| `ruleset_step` | Render the admin step requiring the App-pinned attestation on default. |
| **Module functions** | Definitions in the linked module. |
| `plan_onboarding` | Every step from the project's current state to a ready observe-mode train. |
| `_import_step` | Render commands to import and activate the exact reviewed route bundle. |
| `supported_validation` | `(supported, refused)`: development validation runs only server presets. |
| `_development_plan` | Build the local/development onboarding plan with supported validation and publisher configuration. |

### [src.integration.preflight](../../../src/integration/preflight.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`FunctionalPreflight`** | Blocker codes, plus the `warnings` that never block readiness. |
| `__new__` | Attach nonblocking warnings to the tuple of functional blockers. |
| **Module functions** | Definitions in the linked module. |
| `_resolve` | Await an injected resolver result when necessary. |
| `_definition_scope_matches` | Compare a route's system/project scope with the loaded playbook definition. |
| `_artifact_matches` | Require the installed definition to match the pinned route id, scope, version, and fingerprint. |
| `read_committed_trust_manifest` | `(default-branch SHA, raw manifest bytes)` read through the App client. |
| `daemon_functional_preflight` | Read only the dependencies and repository configuration used at runtime. |

## Background service, durable events, and supporting types

### [src.integration.service](../../../src/integration/service.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`IntegrationService`** | Poll durable integration work without becoming a second authority. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `tick` | Run one control page; the live loop owns at most one remote pass. |
| `_reconcile` | One bounded page per remote source, preserving CI/deadline ordering. |
| `_tick_schedules` | Page due schedules and mark each project's next sweep request due. |
| `_tick_repair_stages` | Page due repair stages and conditionally expire each exact active deadline. |
| `_tick_candidate_ci` | Page pending exact candidate subjects and invoke the configured CI handler. |
| `_tick_intents` | Page unresolved promotion intents and invoke the configured reconciliation handler. |
| `_tick_cleanup` | Page due cleanup items and invoke the configured item handler. |
| `_page` | Advance a fair keyset cursor on scanned rows and wrap when the page is exhausted. |
| `_source` | Isolate a reconciliation source's failure and report passes slower than the tick interval. |
| `_run_optional` | Invoke the optional handler for each durable row, retaining rows when unwired. |
| `_isolated` | Contain one item's exception so unrelated durable work remains retryable. |
| `start` | Start one idempotent background integration reconciliation task. |
| `stop` | Cancel remote/development passes and await the control loop's shutdown. |
| `_run` | Drive bounded background ticks at the configured interval until stopped. |

### [src.integration.outbox](../../../src/integration/outbox.py)

| Scoped name | One-line purpose |
| --- | --- |
| `load_acceptance_state` | Read the frozen destination manifest and its durable continuation. |
| `freeze_destination_manifest` | Persist the first non-empty destination snapshot; concurrent retries reuse it. |
| `advance_acceptance_cursor` | Advance only after the complete page is durable; never regress the cursor. |
| `enqueue_integration_event` | Insert an event on the caller's transaction, idempotently by domain key. |
| **`IntegrationOutbox`** | Deliver one bounded page, acknowledging only durable consumer acceptance. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `dispatch_due` | Try at most one page of due events and return the acknowledged count. |
| `_page` | Freeze a scan boundary so retrying rows cannot monopolize later pages. |
| `_acknowledge` | CAS acceptance of an outbox delivery under its exact nonce. |
| `_retry` | Clear a failed delivery claim and set its bounded next-attempt time. |

### [src.integration.branch_materialization](../../../src/integration/branch_materialization.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`BranchMaterializationService`** | Cut the reserved-but-missing branches for hierarchy/train projects. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `pending_origins` | Live reservations with no branch, oldest first, in enabled projects. |
| `drain_due` | Materialize each pending reservation; never raise into the loop. |
| `_drain` | Materialize pending origins and bootstrap untouched code-bearing container checkpoints. |

### [src.integration.regeneration](../../../src/integration/regeneration.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`RegenerationFailure`** | The scratch-worktree regeneration could not produce a safe tree. |
| `__init__` | Configure the service’s collaborators and retained state. |
| **Module functions** | Definitions in the linked module. |
| `_subprocess_env` | The minimal, worker-scoped environment the regenerator sees. |
| `regenerated_tree` | Materialize *merge_tree_sha* in a scratch worktree, run *command* there, commit the sweep into a fresh tree, and return that tree SHA. |
| `_run` | Regenerate a merged tree in a disposable worktree under a finite process budget. |
| `_kill_process_group` | Terminate the timed-out regeneration process group. |
| `_remove_worktree` | Remove/prune the disposable regeneration worktree with retry handling. |

### [src.integration.migration_heads](../../../src/integration/migration_heads.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`MigrationHead`** | Typed record including `path`, `revision`, `down_revisions`. |
| `scope` | Return the migration file's containing path as its migration stream scope. |
| **Module functions** | Definitions in the linked module. |
| `declaration` | Statically parse migration revision/down_revision assignments without importing code. |
| **`MigrationInspector`** | Use the integration service's retained repository and authenticated Git reads. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `__call__` | Fetch exact reviewed objects and inspect changed migration declarations for head collisions. |
| **Module functions** | Definitions in the linked module. |
| `select_members` | Keep the first ordered member of each revision/sibling-head collision. |

### [src.integration.models](../../../src/integration/models.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`PlaybookRoute`** | Stable activation address plus the exact compiled artifact it resolved. |
| `artifact_matches_route` | Require the pinned artifact's id and scope to match the authored route. |
| `is_available_to_project` | Whether this canonical route may serve `project_id`. |
| **`IntegrationCleanupPolicy`** | Frozen retention and retry limits for post-promotion cleanup. |
| `ordered_backoff` | Require cleanup retry maximum to be at least its initial delay. |
| **Module functions** | Definitions in the linked module. |
| `deprecated_route_fields` | Dotted paths of every deprecated profile field `policy` sets non-null. |

### [src.integration.__init__](../../../src/integration/__init__.py)

Durable hierarchical delivery and integration-train primitives.
`__init__.py` re-exports `BranchKey`, `Fence`, `PromotionInput`, `PromotionValue`, `RepairPolicy`, and `RequiredCheckSet`; it defines no callables.

## Command and CLI surfaces

All state-changing adapters call `CommandHandler`; the typed contracts define allowed principals, outcomes, effects, and replay identity. The following command crosswalk covers the entire `DESIGN_INTEGRATION_COMMANDS` set; local onboarding/setup commands are inventoried with the CLI below.

| Command | Handler | CLI |
| --- | --- | --- |
| `delivery_promote` | `IntegrationCommandsMixin._cmd_delivery_promote` | Generic contract surface. |
| `delivery_receipts` | `IntegrationCommandsMixin._cmd_delivery_receipts` | Generic contract surface. |
| `integration_abort` | `IntegrationCommandsMixin._cmd_integration_abort` | `aq integration abort` |
| `integration_adopt` | `IntegrationCommandsMixin._cmd_integration_adopt` | `aq integration adopt` |
| `integration_adopt_legacy_deliveries` | `IntegrationCommandsMixin._cmd_integration_adopt_legacy_deliveries` | `aq integration adopt-legacy-deliveries` |
| `integration_app_verify` | `IntegrationCommandsMixin._cmd_integration_app_verify` | Generic contract surface. |
| `integration_authorize_root` | `IntegrationCommandsMixin._cmd_integration_authorize_root` | `aq integration authorize-root` |
| `integration_bind_legacy_repositories` | `IntegrationCommandsMixin._cmd_integration_bind_legacy_repositories` | `aq integration bind-legacy-repositories` |
| `integration_build_candidate` | `IntegrationCommandsMixin._cmd_integration_build_candidate` | Generic contract surface. |
| `integration_cancel_preserving` | `IntegrationCommandsMixin._cmd_integration_cancel_preserving` | `aq integration cancel-preserving` |
| `integration_checkpoint_parent` | `IntegrationCommandsMixin._cmd_integration_checkpoint_parent` | Generic contract surface. |
| `integration_ci_evidence` | `IntegrationCommandsMixin._cmd_integration_ci_evidence` | Generic contract surface. |
| `integration_cleanup` | `IntegrationCommandsMixin._cmd_integration_cleanup` | Generic contract surface. |
| `integration_clear_stale_request` | `IntegrationCommandsMixin._cmd_integration_clear_stale_request` | `aq integration clear-stale-request` |
| `integration_close_delivered_pr` | `IntegrationCommandsMixin._cmd_integration_close_delivered_pr` | `aq integration close-delivered-pr` |
| `integration_complete_parent` | `IntegrationCommandsMixin._cmd_integration_complete_parent` | Generic contract surface. |
| `integration_delivery_readiness` | `IntegrationCommandsMixin._cmd_integration_delivery_readiness` | Generic contract surface. |
| `integration_develop` | `IntegrationCommandsMixin._cmd_integration_develop` | `aq integration develop` |
| `integration_development_sweep` | `IntegrationCommandsMixin._cmd_integration_development_sweep` | `aq integration sweep` |
| `integration_eject` | `IntegrationCommandsMixin._cmd_integration_eject` | `aq integration eject` |
| `integration_enable` | `IntegrationCommandsMixin._cmd_integration_enable` | `aq integration enable` |
| `integration_file_children` | `IntegrationCommandsMixin._cmd_integration_file_children` | Generic contract surface. |
| `integration_flush` | `IntegrationCommandsMixin._cmd_integration_flush` | `aq integration flush` |
| `integration_materialize_root` | `IntegrationCommandsMixin._cmd_integration_materialize_root` | `aq integration materialize-root` |
| `integration_migrate_provenance` | `GitCommandsMixin._cmd_integration_migrate_provenance` | `aq integration migrate-provenance` |
| `integration_mutate_hierarchy` | `IntegrationCommandsMixin._cmd_integration_mutate_hierarchy` | Generic contract surface. |
| `integration_parent_verify` | `IntegrationCommandsMixin._cmd_integration_parent_verify` | Generic contract surface. |
| `integration_promote_main` | `IntegrationCommandsMixin._cmd_integration_promote_main` | Generic contract surface. |
| `integration_push_conflict_resolution` | `IntegrationCommandsMixin._cmd_integration_push_conflict_resolution` | Generic contract surface. |
| `integration_rebind_detached_repair` | `IntegrationCommandsMixin._cmd_integration_rebind_detached_repair` | `aq integration rebind-detached-repair` |
| `integration_rebind_repair` | `IntegrationCommandsMixin._cmd_integration_rebind_repair` | `aq integration rebind-repair` |
| `integration_rebind_reused_identity` | `IntegrationCommandsMixin._cmd_integration_rebind_reused_identity` | `aq integration rebind-reused-identity` |
| `integration_reconcile_promotion` | `IntegrationCommandsMixin._cmd_integration_reconcile_promotion` | Generic contract surface. |
| `integration_reconcile_unmaterialized` | `IntegrationCommandsMixin._cmd_integration_reconcile_unmaterialized` | `aq integration reconcile-unmaterialized` |
| `integration_record_noop` | `IntegrationCommandsMixin._cmd_integration_record_noop` | `aq integration record-noop` |
| `integration_record_repair` | `IntegrationCommandsMixin._cmd_integration_record_repair` | Generic contract surface. |
| `integration_recover_candidate_member` | `IntegrationCommandsMixin._cmd_integration_recover_candidate_member` | `aq integration recover-candidate-member` |
| `integration_recover_preserved_repair` | `IntegrationCommandsMixin._cmd_integration_recover_preserved_repair` | `aq integration recover-preserved-repair` |
| `integration_recover_unwritten_resolution` | `IntegrationCommandsMixin._cmd_integration_recover_unwritten_resolution` | Generic contract surface. |
| `integration_redrive_child` | `IntegrationCommandsMixin._cmd_integration_redrive_child` | `aq integration redrive-child` |
| `integration_redrive_root` | `IntegrationCommandsMixin._cmd_integration_redrive_root` | `aq integration redrive-root` |
| `integration_release` | `IntegrationCommandsMixin._cmd_integration_release` | Generic contract surface. |
| `integration_release_delegates` | `IntegrationCommandsMixin._cmd_integration_release_delegates` | `aq integration release-delegates` |
| `integration_release_owner` | `IntegrationCommandsMixin._cmd_integration_release_owner` | `aq integration release-owner` |
| `integration_release_stale_owners` | `IntegrationCommandsMixin._cmd_integration_release_stale_owners` | `aq integration release-stale-owners` |
| `integration_repair_close_current` | `IntegrationCommandsMixin._cmd_integration_repair_close_current` | Generic contract surface. |
| `integration_repair_dispatch` | `IntegrationCommandsMixin._cmd_integration_repair_dispatch` | Generic contract surface. |
| `integration_repair_start` | `IntegrationCommandsMixin._cmd_integration_repair_start` | Generic contract surface. |
| `integration_repair_timeout` | `IntegrationCommandsMixin._cmd_integration_repair_timeout` | Generic contract surface. |
| `integration_reserve_owner` | `IntegrationCommandsMixin._cmd_integration_reserve_owner` | `aq integration reserve-owner` |
| `integration_resolve_candidate_member` | `IntegrationCommandsMixin._cmd_integration_resolve_candidate_member` | `aq integration resolve-candidate-member` |
| `integration_resolve_conflict` | `IntegrationCommandsMixin._cmd_integration_resolve_conflict` | Generic contract surface. |
| `integration_resume` | `IntegrationCommandsMixin._cmd_integration_resume` | `aq integration resume` |
| `integration_retry_cleanup` | `IntegrationCommandsMixin._cmd_integration_retry_cleanup` | `aq integration retry-cleanup` |
| `integration_schedule_due` | `IntegrationCommandsMixin._cmd_integration_schedule_due` | Generic contract surface. |
| `integration_seal` | `IntegrationCommandsMixin._cmd_integration_seal` | Generic contract surface. |
| `integration_settle_parked` | `IntegrationCommandsMixin._cmd_integration_settle_parked` | `aq integration settle-parked` |
| `integration_status` | `IntegrationCommandsMixin._cmd_integration_status` | `aq integration status` |
| `integration_transfer_owner` | `IntegrationCommandsMixin._cmd_integration_transfer_owner` | Generic contract surface. |
| `integration_trust_manifest` | `IntegrationCommandsMixin._cmd_integration_trust_manifest` | Generic contract surface. |
| `integration_waive_history` | `IntegrationCommandsMixin._cmd_integration_waive_history` | `aq integration waive-history` |

Contract definitions and registration: [src/commands/contracts/integration.py](../../../src/commands/contracts/integration.py#L28), [src/commands/contracts/integration.py](../../../src/commands/contracts/integration.py#L3087). CLI presentation may also be generated from registered contracts; the column above names only wrappers declared in `src/cli/integration.py`.

### [src.commands.integration_commands](../../../src/commands/integration_commands.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_failure` | Return a typed unsuccessful integration command result. |
| `_with_reason` | Surface a service refusal's `reason` as the envelope `error`. |
| **`IntegrationCommandsMixin`** | Implemented integration command handlers are registered incrementally. |
| `_cmd_observe_integration_source_ci` | Daemon-only adapter for trusted CI observations, never a raw tool input. |
| `_integration_source_ci_identity` | Hash the exact PR/source/check identity for deduplicated source-CI repairs. |
| `_record_integration_source_ci` | Revalidate server-observed source identity and file/reuse its normal routed CI repair task. |
| `repair_integration_source_ancestry` | Withdraw one exact source whose recorded base is not its ancestor. |
| `_repair_integration_source_ancestry` | Record exact ancestry rejection and reopen that source once with repair feedback. |
| `_integration_task_matches_target` | Require a task's project, repository, and branch to match the requested fence target. |
| `_integration_batch_matches_target` | Require a batch's project, repository, and integration branch to match the target. |
| `_integration_collector_matches_target` | Resolve collector identity through its durable batch or operation relationship. |
| `_integration_operation_matches_target` | Match the operation's parent or root batch to its permitted branch target. |
| `_integration_repair_task_matches_target` | Require the exact active repair delegate and its operation's target binding. |
| `_integration_destination_matches_target` | Validate the proposed successor owner according to its persisted owner role. |
| `_cmd_integration_transfer_owner` | Fence out one branch writer only after a proven server-side handoff. |
| `_integration_delivery_authorized` | Require local/service authority or the exact scoped live supervisor/playbook capability. |
| `_integration_promotion_service` | Resolve the daemon's parent PromotionService or construct the command fallback. |
| `_integration_scheduler` | Resolve the shared scheduler or construct a database-backed fallback. |
| `_integration_control_service` | Resolve the daemon's control service with scheduling, cleanup, and legacy proof hooks. |
| `_integration_operator_for_operation` | Resolve operation scope and require a local operator or live project supervisor. |
| `_integration_operator_for_batch` | Resolve batch scope and require a local operator or live project supervisor. |
| `_cmd_integration_status` | Authorize project status and return full observations or the requested control-only projection. |
| `_integration_app_inputs` | `(refusal, inputs)` shared by the read-only App-mode commands. |
| `_cmd_integration_trust_manifest` | Render the App-mode trust manifest and compare the default-branch copy. |
| `_cmd_integration_app_verify` | Check everything App-mode runtime depends on, one item per concern. |
| `_cmd_integration_flush` | Recover interrupted completion and request a strict sweep or development publication pass. |
| `_cmd_integration_eject` | Authorize batch-member ejection and freshly prove any retained rejected repair ref. |
| `_reconcile_integration_completion` | Recover stopped closed owners and exact lost root PR links before manual flush. |
| `_cmd_integration_enable` | Authorize and CAS a readiness-checked integration mode transition. |
| `_cmd_integration_waive_history` | Authorize an audited waiver for the exact current historical blocker digest. |
| `_cmd_integration_reconcile_unmaterialized` | Authorize and CAS pristine pre-rollout task/origin reconciliation. |
| `_cmd_integration_resume` | Authorize safe rearming of the exact human-required operation. |
| `_cmd_integration_abort` | Authorize explicit safe operation cancellation with an audit reason. |
| `_cmd_integration_retry_cleanup` | Authorize safe cleanup retry or materialization for the exact batch. |
| `_cmd_integration_release_delegates` | Settle the delegates of one operation that already ended. |
| `_cmd_integration_recover_candidate_member` | Recover a durable pushed root-candidate repair. |
| `_cmd_integration_release_owner` | Run the fenced owner-recovery check for one owner or task. |
| `_cmd_integration_reserve_owner` | Restore one stopped producer's missing canonical branch reservation. |
| `_cmd_integration_release_stale_owners` | Release a project's provably safe reserved owners; report every other. |
| `_cmd_integration_clear_stale_request` | Classify a project's outstanding sweep request; release it if it can never end. |
| `_cmd_integration_redrive_root` | Diagnose a completed train root's missing PR; open it for the reported head. |
| `_cmd_integration_materialize_root` | Prove and record a completed legacy root's PR source identity. |
| `_cmd_integration_authorize_root` | Record an operator's authorization of one exact completed train root source. |
| `_cmd_integration_redrive_child` | Diagnose a completed child its parent never assembled; advance it for the head. |
| `_cmd_integration_rebind_reused_identity` | Prove a task's inherited integration identity; rebind it once settled. |
| `_cmd_integration_rebind_repair` | Prove a live repair candidate and reserve it under the current intent. |
| `_cmd_integration_recover_preserved_repair` | Consume an audited completed candidate without renewing repair authority. |
| `_cmd_integration_rebind_detached_repair` | Rebind a detached debug stage frozen on an unpublished head to its conflict. |
| `_cmd_integration_adopt_legacy_deliveries` | Record provable pre-train deliveries of children no train will collect. |
| `_cmd_integration_bind_legacy_repositories` | Bind terminal hierarchy members with designated-repository delivery proof. |
| `_cmd_integration_close_delivered_pr` | Close one open PR only once Git proves its work is on the default branch. |
| `_integration_train_service` | Resolve/build TrainService with effective mode policy and migration-head inspection. |
| `_integration_root_promotion_service` | Resolve/build RootPromotionService with authenticated attestation proof. |
| `_integration_cleanup_service` | Resolve/build the cleanup executor using the daemon Git and App transports. |
| `_integration_candidate_service` | Resolve/build a repository-bound candidate service with exact ownership handoff proof. |
| `_integration_release_service` | Resolve/build the terminal-batch scheduling release service. |
| `_cmd_integration_schedule_due` | Authorize and validate a project sweep request before marking it due. |
| `_cmd_integration_seal` | Authorize and validate sealing of the exact project/request frontier. |
| `_cmd_integration_promote_main` | Authorize promotion of the server-resolved exact green batch revision. |
| `_cmd_integration_cleanup` | Authorize batch cleanup, materialize missing identities, and advance due work. |
| `_cmd_integration_build_candidate` | Authorize the current batch build and apply its frozen moved-main rebuilding policy. |
| `_cmd_integration_repair_close_current` | Resolve a closed root writer only while its adopted revision is current. |
| `_cmd_integration_ci_evidence` | Authorize exact candidate CI observation and classify pending/red/green attestation results. |
| `_cmd_integration_release` | Authorize release of the exact terminal root batch's request and lease. |
| `_hierarchy_integration_service` | Build the hierarchy facade with exact remote-head, checkpoint, and materialization proofs. |
| `_integration_repair_service` | Resolve/build RepairService with provider-backed stop, handoff, and owner recovery. |
| `_integration_operation_project_id` | Resolve repair scope through its parent live/archive task or root batch. |
| `_repair_command_authorized` | Require the command capability in the operation's resolved project scope. |
| `_cmd_integration_repair_start` | Authorize and parse activation of the exact bounded repair operation. |
| `_cmd_integration_record_repair` | Authorize and count exact repair evidence under the current stage budget. |
| `_cmd_integration_repair_timeout` | Authorize conditional expiration of the exact active stage deadline. |
| `_cmd_integration_repair_dispatch` | Authorize and dispatch the server-resolved current repair stage and subject. |
| `_cmd_integration_file_children` | Authorize checked child filing against the parent's expected generation. |
| `_cmd_integration_checkpoint_parent` | Authorize checked parent generation/head checkpointing. |
| `_cmd_integration_mutate_hierarchy` | Authorize movement of unsealed work and map typed hierarchy refusals. |
| `_cmd_integration_delivery_readiness` | Authorize parent readiness evaluation and map ready/waiting/failed outcomes. |
| `_cmd_integration_record_noop` | Authorize no-code/waived disposition and independently prove any asserted source equivalence. |
| `_cmd_integration_parent_verify` | Authorize exact parent verification from recorded generation/head CI evidence. |
| `_cmd_integration_complete_parent` | Authorize completion or idempotent replay of the exact verified parent. |
| `_cmd_delivery_promote` | Validate collector/source/target authority, prepare the reviewed squash, and perform its fenced push. |
| `_cmd_integration_reconcile_promotion` | Authorize exact intent reconciliation into observed parent delivery evidence. |
| `_cmd_integration_resolve_conflict` | Reserve only the attached repair writer's exactly proved conflict-resolution candidate. |
| `_cmd_integration_push_conflict_resolution` | Push only the current writer's previously frozen resolution reservation. |
| `_cmd_integration_resolve_candidate_member` | Resolve only the candidate conflict assigned to the authenticated writer. |
| `_cmd_integration_recover_unwritten_resolution` | Operator-only recovery for a malformed reservation with no write attempt. |
| `_cmd_delivery_receipts` | Resolve source scope server-side and return its durable delivery receipts. |
| `_promotion_result` | Serialize PromotionValue with command outcome, success, and optional error. |
| `_development_integration` | Resolve/build the development publisher with finite managed validation jobs. |
| `_cmd_integration_develop` | Authorize development configuration and verify the App can pass target protection. |
| `_cmd_integration_adopt` | Authorize explicit fenced Git delivery/completion equivalence for named tasks. |
| `_cmd_integration_development_sweep` | Authorize immediate publication or supervised recovery of one named child. |
| `_cmd_integration_settle_parked` | Authorize reasoned settlement/dismissal of one parked development operation. |
| `_cmd_integration_cancel_preserving` | Authorize operation cancellation that preserves refs and stopped/unknown writer work. |

### [src.commands.contracts.integration](../../../src/commands/contracts/integration.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`IntegrationReleaseOwnerArgs`** | Typed command arguments including `task_id`, `owner_row_id`, `dry_run`. |
| `exactly_one_target` | Require exactly one owner-row or task selector. |
| **`IntegrationReleaseStaleOwnersArgs`** | Typed command arguments including `project_id`, `dry_run`, `older_than`. |
| `older_than_is_a_duration` | Validate the operator's quiet-time threshold with the shared duration parser. |
| **`IntegrationClearStaleRequestArgs`** | Typed command arguments including `project_id`, `dry_run`, `expected_request_id`, `reason`. |
| `applying_names_the_request_and_a_reason` | Require the previewed request id and audit reason for apply. |
| **`IntegrationRedriveRootArgs`** | Typed command arguments including `task_id`, `dry_run`, `expected_head_sha`, `reason`. |
| `expected_head_is_a_commit` | Require a valid exact commit id for the root preview/apply fence. |
| `applying_names_the_head_and_a_reason` | Require the previewed head and reason for root redrive apply. |
| **`IntegrationRedriveChildArgs`** | Typed command arguments including `task_id`, `dry_run`, `expected_head_sha`, `reason`. |
| `expected_head_is_a_commit` | Require a valid exact commit id for child preview/apply fencing. |
| `applying_names_the_head_and_a_reason` | Require the previewed child head and audit reason for apply. |
| **`IntegrationRebindReusedIdentityArgs`** | Typed command arguments including `task_id`, `dry_run`, `expected_origin_ids`, `discard_tips`, `reason`. |
| `discard_tips_are_exact_commits` | Require canonical full commit ids for explicit predecessor discards. |
| `applying_names_the_origins_and_a_reason` | Require the complete previewed origin-id set and audit reason for apply. |
| **`IntegrationRebindRepairArgs`** | Typed command arguments including `task_id`, `dry_run`, `expected_head_sha`. |
| `apply_requires_proved_head` | Require the exact valid previewed commit for live repair rebind apply. |
| **`IntegrationRecoverPreservedRepairArgs`** | Typed command arguments including `operation_id`, `intent_id`, `candidate_sha`, `dry_run`, `expected_stage`. |
| `require_exact_recovery` | Require exact candidate/preserved identities and an audit reason for recovery apply. |
| **`IntegrationRebindDetachedRepairArgs`** | Typed command arguments including `operation_id`, `dry_run`, `expected_stage`, `expected_remote_head_sha`, `reason`. |
| `apply_requires_proved_identity` | Require previewed stage, delegate, head, intent, and reason for detached rebind apply. |
| **`IntegrationAdoptLegacyDeliveriesArgs`** | Typed command arguments including `project_id`, `dry_run`, `accept`, `retire`, `supersede`. |
| `supersede_names_commits` | Require exact canonical commit ids for explicit legacy supersession decisions. |
| `decisions_need_reason` | Require an audit reason for applying adoption, supersession, or reopen decisions. |
| **`IntegrationBindLegacyRepositoriesArgs`** | Typed command arguments including `project_id`, `dry_run`, `reason`. |
| `apply_needs_reason` | Require an audit reason for applying legacy repository binding. |
| **`IntegrationCloseDeliveredPrArgs`** | Typed command arguments including `project_id`, `pr_number`, `dry_run`, `expected_head_sha`, `reason`. |
| `expected_head_is_a_commit` | Require a valid exact PR head for closure preview/apply fencing. |
| `applying_names_the_head_and_a_reason` | Require the previewed PR head and audit reason before closure apply. |
| **`IntegrationResolveCandidateMemberArgs`** | Publish the exact repair produced by this session's candidate-member assignment. |
| `exact_git_oid` | Require valid full Git object ids for the candidate repair reservation. |
| `exact_repair_commits` | Require a nonempty ordered list of exact valid repair commits. |
| **`IntegrationRecordNoopArgs`** | Typed command arguments including `child_task_id`, `expected_head_sha`. |
| `exact_git_oid` | Require exact Git ids for any asserted no-code source proof. |
| **`IntegrationRepairStartArgs`** | Typed command arguments including `operation_id`, `starting_sha`, `trigger_id`. |
| `exact_git_oid` | Require a valid exact starting SHA for the repair stage. |
| **`IntegrationRepairDispatchArgs`** | Typed command arguments including `operation_id`, `stage`, `batch_id`, `revision`, `head_sha`. |
| `complete_candidate_subject` | Require revision/head together when a caller supplies a candidate subject. |
| **Module functions** | Definitions in the linked module. |
| `_operational_contract` | Construct typed operator-control contracts with outcomes, principal/effect subjects, and idempotency. |
| `_repair_contract` | Construct operation/stage repair contracts with frozen subject and finite outcome semantics. |
| `_root_subject_contract` | Construct exact batch/revision command contracts with server-owned authority and effect clauses. |
| `_parent_contract` | Construct parent-generation command contracts with their read/update semantics. |
| `_transfer_adapter` | Invoke ownership transfer under the command principal and validate its typed result/outcome. |
| `_invoke_adapter` | Adapt legacy delivery handlers to typed command values, outcomes, and principal context. |
| `_promote_adapter` | Validate typed `delivery_promote` results and principal context. |
| `_receipts_adapter` | Validate typed `delivery_receipts` results and principal context. |
| `_reconcile_adapter` | Validate typed `integration_reconcile_promotion` results and principal context. |
| `_resolve_conflict_adapter` | Validate typed `integration_resolve_conflict` results and principal context. |
| `_push_conflict_resolution_adapter` | Validate typed `integration_push_conflict_resolution` results and principal context. |
| `_resolve_candidate_member_adapter` | Validate typed `integration_resolve_candidate_member` results and principal context. |
| `_promote_main_adapter` | Validate typed `integration_promote_main` results and principal context. |
| `_cleanup_adapter` | Validate typed `integration_cleanup` results and principal context. |
| `_build_candidate_adapter` | Validate typed `integration_build_candidate` results and principal context. |
| `_repair_close_current_adapter` | Validate typed `integration_repair_close_current` results and principal context. |
| `_ci_evidence_adapter` | Validate typed `integration_ci_evidence` results and principal context. |
| `_release_adapter` | Validate typed `integration_release` results and principal context. |
| `_hierarchy_adapter` | Adapt legacy hierarchy/control handlers to typed command values, outcomes, and principal context. |
| `_file_children_adapter` | Validate typed `integration_file_children` results and principal context. |
| `_checkpoint_parent_adapter` | Validate typed `integration_checkpoint_parent` results and principal context. |
| `_mutate_hierarchy_adapter` | Validate typed `integration_mutate_hierarchy` results and principal context. |
| `_delivery_readiness_adapter` | Validate typed `integration_delivery_readiness` results and principal context. |
| `_parent_verify_adapter` | Validate typed `integration_parent_verify` results and principal context. |
| `_record_noop_adapter` | Validate typed `integration_record_noop` results and principal context. |
| `_complete_parent_adapter` | Validate typed `integration_complete_parent` results and principal context. |
| `_repair_start_adapter` | Validate typed `integration_repair_start` results and principal context. |
| `_repair_dispatch_adapter` | Validate typed `integration_repair_dispatch` results and principal context. |
| `_record_repair_adapter` | Validate typed `integration_record_repair` results and principal context. |
| `_repair_timeout_adapter` | Validate typed `integration_repair_timeout` results and principal context. |
| `_schedule_due_adapter` | Validate typed `integration_schedule_due` results and principal context. |
| `_seal_adapter` | Validate typed `integration_seal` results and principal context. |
| `_status_adapter` | Validate typed `integration_status` results and principal context. |
| `_trust_manifest_adapter` | Validate typed `integration_trust_manifest` results and principal context. |
| `_app_verify_adapter` | Validate typed `integration_app_verify` results and principal context. |
| `_flush_adapter` | Validate typed `integration_flush` results and principal context. |
| `_eject_adapter` | Validate typed `integration_eject` results and principal context. |
| `_enable_adapter` | Validate typed `integration_enable` results and principal context. |
| `_reconcile_unmaterialized_adapter` | Validate typed `integration_reconcile_unmaterialized` results and principal context. |
| `_waive_history_adapter` | Validate typed `integration_waive_history` results and principal context. |
| `_resume_adapter` | Validate typed `integration_resume` results and principal context. |
| `_abort_adapter` | Validate typed `integration_abort` results and principal context. |
| `_retry_cleanup_adapter` | Validate typed `integration_retry_cleanup` results and principal context. |
| `_release_delegates_adapter` | Validate typed `integration_release_delegates` results and principal context. |
| `_release_owner_adapter` | Validate typed `integration_release_owner` results and principal context. |
| `_reserve_owner_adapter` | Validate typed `integration_reserve_owner` results and principal context. |
| `_release_stale_owners_adapter` | Validate typed `integration_release_stale_owners` results and principal context. |
| `_clear_stale_request_adapter` | Validate typed `integration_clear_stale_request` results and principal context. |
| `_redrive_root_adapter` | Validate typed `integration_redrive_root` results and principal context. |
| `_materialize_root_adapter` | Validate typed `integration_materialize_root` results and principal context. |
| `_authorize_root_adapter` | Validate typed `integration_authorize_root` results and principal context. |
| `_redrive_child_adapter` | Validate typed `integration_redrive_child` results and principal context. |
| `_rebind_reused_identity_adapter` | Validate typed `integration_rebind_reused_identity` results and principal context. |
| `_rebind_repair_adapter` | Validate typed `integration_rebind_repair` results and principal context. |
| `_recover_preserved_repair_adapter` | Validate typed `integration_recover_preserved_repair` results and principal context. |
| `_rebind_detached_repair_adapter` | Validate typed `integration_rebind_detached_repair` results and principal context. |
| `_adopt_legacy_deliveries_adapter` | Validate typed `integration_adopt_legacy_deliveries` results and principal context. |
| `_close_delivered_pr_adapter` | Validate typed `integration_close_delivered_pr` results and principal context. |
| `_bind_legacy_repositories_adapter` | Validate typed `integration_bind_legacy_repositories` results and principal context. |
| `_recover_candidate_member_adapter` | Validate typed `integration_recover_candidate_member` results and principal context. |
| `_development_adapter` | Create a typed adapter for a development operator command. |
| `register_integration_contracts` | Validate typed `integration_migrate_provenance` results and principal context. |
| `_recover_unwritten_resolution_adapter` | Validate typed `integration_recover_unwritten_resolution` results and principal context. |

### [src.cli.integration](../../../src/cli/integration.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_execute` | Call the shared API client command endpoint and emit the versioned CLI result. |
| `integration` | Inspect and control hierarchical integration trains. |
| `integration_status` | Show rollout, readiness, active work, and cleanup for PROJECT_ID. |
| `integration_record_noop` | Record a verified no-code receipt for a completed child task. |
| `integration_resolve_candidate_member` | Resolve the candidate member assigned to this repair session. |
| `integration_flush` | Request an immediate eligibility pass or train sweep for PROJECT_ID. |
| `integration_eject` | Remove one epic from a sealed batch while retaining its approval. |
| `integration_enable` | CAS PROJECT_ID to MODE using the generation reported by status. |
| `integration_reconcile_unmaterialized` | Bind safe pre-rollout tasks and reserve their hierarchy origins. |
| `integration_waive_history` | Waive only the exact historical blockers reported for PROJECT_ID. |
| `integration_resume` | Resume a safe, human-required integration OPERATION_ID. |
| `integration_abort` | Abort a safe, human-required integration OPERATION_ID. |
| `integration_retry_cleanup` | Requeue the exact safe cleanup items for BATCH_ID. |
| `integration_release_owner` | Release a stranded branch owner after proving its writer and branch safe. |
| `integration_reserve_owner` | Restore a stopped train task's missing canonical branch reservation. |
| `integration_release_stale_owners` | Release a project's stale reserved branch owners that are provably safe. |
| `integration_adopt_legacy_deliveries` | Adopt delivered children of parents that finished outside the train. |
| `integration_bind_legacy_repositories` | List terminal hierarchy members with repository delivery proof, then bind them. |
| `integration_close_delivered_pr` | Close open PR PR_NUMBER only once Git proves its work is on the default branch. |
| `integration_clear_stale_request` | Say whether PROJECT_ID's outstanding sweep request can still end; free it if not. |
| `integration_redrive_root` | Say why completed train root TASK_ID has no pull request; open it if it should. |
| `integration_materialize_root` | Prove a completed legacy train root's PR head and record its missing identity. |
| `integration_authorize_root` | Authorize completed train root TASK_ID's exact source for delivery. |
| `integration_redrive_child` | Say why completed child TASK_ID was never assembled into its parent; advance it. |
| `integration_rebind_reused_identity` | Prove a task's inherited branch origin was delivered; retire it if so. |
| `integration_rebind_repair` | Prove a live delegate's candidate against its current conflict intent. |
| `integration_recover_preserved_repair` | Recover a completed parent resolution preserved by owner recovery. |
| `integration_rebind_detached_repair` | Rebind OPERATION_ID's detached debug stage to its conflict at the published head. |
| `integration_release_delegates` | Settle the delegate tasks of an OPERATION_ID that has already ended. |
| `integration_recover_candidate_member` | Resolve one pushed frozen candidate-member repair reservation. |
| `integration_develop` | Use automatic development batches with explicit local validation. |
| `integration_adopt` | Record already-delivered work without replaying old repair checkpoints. |
| `integration_development_sweep` | Build and publish a development batch now. |
| `integration_settle_parked` | Settle a parked development delivery as not owed, or dismiss it. |
| `integration_migrate_provenance` | Inventory legacy completion generations and exact repair bindings in Git. |
| `integration_cancel_preserving` | Cancel obsolete repair scheduling while retaining refs and attached workspaces. |
| `_git` | Read repository objects through a checked synchronous Git command in the CLI. |
| `_read_repository` | `(commit, workflows, stack files)` at `ref`. |
| `_daemon_config` | Read optional local daemon YAML configuration for onboarding suggestions. |
| `_github_app_id` | Read the configured numeric App id for rendered onboarding/setup steps. |
| `_gh_json` | One best-effort `gh` read; `None` when gh is absent or refuses. |
| `_render_onboarding` | Render the inventory, warnings, generated files, and ordered operator plan. |
| `integration_onboard_train` | Plan PROJECT_ID's move onto the integration train; print every step. |
| `_trust_manifest_write_command` | Render the exact command writing the plan's trust manifest. |
| `_check_verdict` | `(passes, warning codes)` for `--check`: identity decides, the check set warns. |
| `_render_diff` | Render expected/committed manifest field differences. |
| `_render_trust_manifest` | Render manifest bytes, committed comparison, and optional check verdict. |
| `integration_trust_manifest` | Render PROJECT_ID's App-mode trust manifest (.github/agent-queue-integration.json). |
| `_app_mode_args` | Load/validate optional policy JSON and repository arguments for App diagnostics. |
| `_app_verify` | Call the read-only integration_app_verify command through the API client. |
| `_app_item` | Find one named readiness item in the App evaluation result. |
| `_compact` | Serialize compact deterministic diagnostic JSON. |
| `_render_app_item` | Render one App readiness observation with expected values and remediation. |
| `_render_app_verify` | Render the aggregate App readiness report. |
| `integration_app_verify` | Check everything PROJECT_ID's App credential mode depends on. |
| `_gh_variable_set` | Build the exact gh command for one repository Actions variable. |
| `_differing_variables` | Names whose value differs from the expected one; `None` when unread. |
| `_run_gh` | Execute an explicitly requested administrative gh setup command and retain its outcome. |
| `_ruleset_commands` | Render exact ruleset update/create commands from the verified setup plan. |
| `_render_app_setup` | Render read-only remediation and only eligible explicit admin setup commands. |
| `integration_app_setup` | Print, per App-mode concern, what is wrong and the exact fix. |
| `_shlex_join` | Shell-quote the rendered operator command arguments. |

### [src.commands.ci_commands](../../../src/commands/ci_commands.py)

| Scoped name | One-line purpose |
| --- | --- |
| `failure_signature` | A stable digest of *what* is red, independent of *which commit* is red. |
| `_key_parts` | `(signature, n)` of a `ci-baseline:<signature>:<n>` key, else `None`. |
| `_key_signature` | Extract the baseline failure signature from a repair dedup key. |
| `_owned` | The part of a failure that *attempt* owns. |
| `plan_repair` | Decide which repair owns the current failure, and what a new one would own. |
| `render_repair_task` | The title and description of the repair task the sentinel files. |
| `_string_list` | Validate, normalize, and deduplicate a stored list of failing names. |
| **`CiCommandsMixin`** | Mixin that adds CI baseline reads to CommandHandler. |
| `_observe_ci_baseline` | Read *ref*'s head check runs and, when red, its failing pytest node ids. |
| `_ci_repair_attempts` | Every `ci-baseline:*` repair in the project, oldest first, with its record. |
| `_cmd_ci_baseline_status` | Judge a branch head's CI and derive the repair task for it. |
| `_cmd_ci_repair_adopt` | Make a live task the repair that owns a red branch's failure. |

## Durable query and schema support

### [src.database.queries.integration_control_queries](../../../src/database/queries/integration_control_queries.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_mode` | Validate one of the supported persisted hierarchical integration modes. |
| `_digest` | Require a canonical SHA-256 blocker digest for history-waiver identity. |
| **`IntegrationControlQueriesMixin`** | Durable primitives composed by the later hierarchy-locked cutover. |
| `cas_project_integration_control_on` | CAS the project projection; callers append audit in the same transaction. |
| `append_integration_rollout_transition_on` | Append one exact post-CAS transition record without rewriting history. |
| `append_integration_history_waiver_on` | Append immutable operator waiver evidence for the exact project/generation/blocker digest. |
| `consume_integration_history_waiver_on` | Consume a matching waiver once without mutating its original record. |
| `consume_integration_history_waiver` | Consume an exact history waiver inside a new transaction. |
| `append_integration_legacy_gate_applicability_on` | Append waiver applicability without resolving or deleting the gate. |
| `set_integration_legacy_suppression_on` | Set the reversible per-project legacy-routing projection. |
| `get_integration_legacy_suppression` | Read the saved legacy controls suppressed during hierarchy/train mode. |
| `list_integration_legacy_suppressions` | Return the small per-project routing predicate snapshot. |

### [src.database.queries.integration_delivery_queries](../../../src/database/queries/integration_delivery_queries.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`IntegrationDeliveryQueriesMixin`** | Backend-neutral promotion operations with transactional idempotency. |
| `append_integration_review_evidence` | Validate and append immutable exact-source review evidence under the project lock. |
| `get_active_integration_verifier_for_task` | Read the current verifier assignment for a task's live parent episode. |
| `get_integration_verifier_operation` | Return the verifier binding even after its parent operation completes. |
| `get_active_parent_integration_operation` | Read the parent checkpoint's active integration operation. |
| `get_applicable_integration_review_evidence` | Return the one current approved exact tuple, never an older approval. |
| `get_integration_review_evidence` | Read one immutable source review evidence record. |
| `get_task_branch_origin_for_promotion` | Read the task's unretired immutable source branch origin. |
| `reserve_integration_promotion_intent` | Reserve a domain identity and receipt before Git construction. |
| `get_integration_promotion_intent` | Read one durable parent/root promotion intent. |
| `reserve_integration_conflict_resolution` | Freeze one conflicted intent's agent-authored resolution identity. |
| `recover_unwritten_conflict_resolution_on` | Supersede one never-written resolution and create its fresh successor. |
| `record_integration_resolution_push_on` | Persist one stable post-push observation and its lifecycle fact. |
| `mark_integration_resolution_push_started_on` | Persist the pre-write fence for one resolution push attempt. |
| `authorize_legacy_resolution_recovery_on` | Turn one legacy unknown-start reservation into a newly fenced attempt. |
| `mark_integration_promotion_prepared` | CAS an intent to prepared with its immutable candidate and recovery ref. |
| `mark_integration_promotion_conflict` | CAS an intent to conflict with bounded durable diagnostics. |
| `mark_integration_promotion_pushed` | Record that the prepared intent's external push was attempted/applied. |
| `finalize_integration_promotion` | Insert receipt plus delivery/cleanup events in one transaction. |
| `_finalize_integration_promotion_on` | Finalize under the caller's project/branch fence when recovering a write. |
| `list_integration_delivery_receipts` | Read ordered parent or root delivery receipts for the requested subject. |
| `_promotion_intent_by_domain` | Read the idempotent promotion intent for its immutable domain key. |
| `_unresolved_target_intent` | Find an unresolved promotion already owning the target transition. |
| `_locked_intent` | Lock and require the durable promotion intent. |
| `_assert_same_intent` | Reject conflicting replay of immutable promotion identity fields. |

### [src.database.queries.integration_reconciliation_queries](../../../src/database/queries/integration_reconciliation_queries.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_require_limit` | Require a positive bounded reconciliation page size. |
| **`IntegrationReconciliationQueriesMixin`** | Stable keyset pages; callers own transitions and cursor advancement. |
| `due_integration_schedule_page` | Select due enabled schedules in fair due-time/project keyset order. |
| `due_integration_repair_stage_page` | Select exact active stages whose absolute deadlines are due. |
| `pending_candidate_ci_page` | Select current candidates awaiting CI or unfinished attestation publication. |
| `unresolved_integration_intent_page` | Select unresolved promotion intents in update-time/id keyset order. |
| `pending_integration_cleanup_page` | Select due normalized cleanup items in retry-time/domain keyset order. |

### [src.database.queries.integration_schedule_queries](../../../src/database/queries/integration_schedule_queries.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`IntegrationScheduleQueriesMixin`** | Lock and mutate one schedule without opening an independent connection. |
| `lock_integration_schedule_on` | Lock/create the project's durable sweep schedule row. |
| `update_integration_schedule_on` | CAS schedule fields under its expected generation/version. |

### [src.database.queries.integration_state_queries](../../../src/database/queries/integration_state_queries.py)

| Scoped name | One-line purpose |
| --- | --- |
| `session_attached_clause` | A `sessions` row that may still write for the task it names. |
| **`IntegrationStateQueriesMixin`** | Integration-state reads; state mutations stay caller-transaction owned. |
| `get_terminal_integration_delegate_operation` | Resolve obsolete repair/verifier work even while its owner is retained. |
| `get_retired_integration_writer` | Prove that every integration writer seat *task_id* held is gone for good. |
| `get_integration_delegate_cleanup` | Name what a delegate still holds, without releasing any of it. |
| `get_integration_checkpoint` | Read the task's durable generation, branch head, and episode checkpoint. |
| `get_integration_batch` | Read one durable root-train batch. |
| `get_integration_operation` | Read one parent/root repair operation. |
| `get_integration_operation_artifact_route` | Return an operation's immutable owner route, if it has one. |
| `get_active_integration_repair_for_task` | Resolve one repair task's current active, nonterminal operation. |
| `get_parent_repair_prime_context` | Read the current conflict and attached fence, never a historical dossier. |
| `get_repair_filing_scope` | Resolve a delegate's server-owned logical filing scope, including expiry. |

### [src.database.queries.integration_train_queries](../../../src/database/queries/integration_train_queries.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_root_delivery_receipt_conditions` | The receipt rules shared by eligibility and dependency satisfaction. |
| **`IntegrationTrainQueriesMixin`** | Exact frontier reads that retain the caller's lock and transaction. |
| `eligible_root_page_on` | Select exact completed reviewed roots whose route, gates, origin, and delivery state allow sealing. |
| `delivered_root_task_ids_on` | Return epic ids delivered to this repository's default branch. |
| `latest_exact_reviews_on` | Read each requested source identity's latest exact review verdict. |

### Durable tables and journals

Source: [src/database/tables.py](../../../src/database/tables.py). Development operations use revisions in the existing event log; Git completion/replacement refs carry source provenance.

| Table or record family | Purpose |
| --- | --- |
| `task_metadata` | Store task-scoped integration policy, CI repair ownership, and migration annotations. |
| `integration_source_ci` | Cache generation-bound source CI evidence and bounded repair history for one reviewed source. |
| `events` | Retain lifecycle facts and revisioned development-publication journals. |
| `task_completion_records` | Retain a task close's work outcome, branch, commits, PR, and verification separately from target delivery. |
| `playbook_pending_events` | Durably accept pinned policy events with protected retry and dispatch claims. |
| `task_integration_checkpoints` | Track the parent's checkpoint, generation, collection/verifier state, episode, and completed verification identities. |
| `task_branch_origins` | Record reserved/materialized branch provenance and audited obsolete-branch discard progress. |
| `integration_branch_owners` | Fence repository/ref writers by owner role, session, workspace, and lease expiry. |
| `integration_review_evidence` | Bind a review verdict to exact source base/head/tree, generation, and reviewer identity. |
| `integration_root_authorizations` | Record an operator's generation-bound root-source authorization and reason. |
| `integration_promotion_intents` | Journal exact source-to-target promotion, expected ref, recovery ref, resolution reservation, and immutable writer authority. |
| `task_delivery_receipts` | Record proved delivery of a reviewed source to a target, including batch, disposition, and parent episode provenance. |
| `integration_batches` | Freeze a root train's manifest, policy/artifact snapshot, current candidate, main delivery, and cleanup lifecycle. |
| `integration_batch_members` | Retain the ordered frozen source identities and review evidence included in a root train. |
| `integration_candidate_revisions` | Checkpoint one candidate construction base, manifest, next member, head, and CI state. |
| `integration_candidate_member_results` | Journal each candidate member's exact merge input, generated squash, and conflict/result evidence. |
| `integration_candidate_publications` | Journal the exact candidate audit-ref push and idempotent audit PR identity. |
| `integration_candidate_resolutions` | Reserve an exact repair writer, bounded stage, candidate member, resolution head, and handoff proof. |
| `integration_candidate_ref_mutations` | Journal fenced candidate/ref writes with expected head, desired head, nonce, and irreversible prewrite marker. |
| `integration_root_intent_members` | Pin the ordered source/member and receipt evidence authorized by a root-main promotion intent. |
| `integration_repair_operations` | Own one parent/batch repair episode, frozen policy, active stage, verifier, and pinned policy route. |
| `integration_parent_episodes` | Name a parent's immutable collection episode and pre-collection generation/checkpoint. |
| `integration_child_dispositions` | Revision a child's include/drop disposition under a specific parent operation and episode. |
| `integration_repair_stages` | Retain finite repair budgets, deadlines, current/success subjects, delegates, dossiers, and workspace handoffs. |
| `integration_delegate_releases` | Audit exact operation-owned delegate retirement and its preserved cleanup references. |
| `integration_owner_recoveries` | Audit stopped-owner recovery decisions and evidence for each fenced ref. |
| `integration_check_evidence` | Store authenticated exact-subject check attempts, required-check versions, classifications, and aggregate conclusions. |
| `integration_attestation_publications` | Journal the candidate's App check-run or daemon-receipt publication under a nonce and exact evidence identity. |
| `integration_cleanup_items` | Journal each batch cleanup item with expected identity, claim/retry state, and irreversible-write evidence. |
| `integration_repair_stage_evidence` | Deduplicate check evidence and retain its counted attempt, outcome, and action under the original stage. |
| `integration_parent_verifications` | Bind successful parent verification to one operation, episode, generation, head, and required-check version. |
| `integration_parent_operation_completions` | Deduplicate exact parent-operation completion against its successful verification. |
| `integration_episode_receipt_acceptances` | Audit ancestry-proved reuse of an earlier episode's receipt in a current parent operation. |
| `integration_parent_verification_evidence` | Link a parent verification to its authenticated check evidence. |
| `integration_operation_artifact_pins` | Retain reviewed playbook artifacts needed to resume an operation's frozen policy. |
| `epic_dependencies` | Persist declared container dependencies separately from leaf work dependencies. |
| `project_integration_schedules` | Track timer/settling due windows, one outstanding request, and one coalesced catchup request. |
| `project_integration_leases` | Exclude concurrent train writers with a per-project/repository owner and monotonic fence. |
| `integration_release_results` | Deduplicate exact terminal-batch release and retain any coalesced catchup identity. |
| `integration_history_waivers` | Audit a reasoned operator waiver against a specific migration blocker digest. |
| `integration_rollout_transitions` | Audit rollout mode/generation changes, draining, legacy suppression, and the exact blockers/waiver consumed. |
| `integration_history_waiver_consumptions` | Bind a one-time historical waiver to the exact rollout transition and blocker digest. |
| `integration_legacy_gate_applicability` | Record which pre-train gates remain applicable during an audited rollout. |
| `integration_legacy_suppression` | Version the effective suppression of legacy merge sweeps, final-review routes, and gate creation. |
| `integration_outbox` | Deduplicate durable integration facts and checkpoint acceptance by each frozen destination. |
| `integration_outbox_artifact_pins` | Keep policy artifacts reachable while an outbox event owes delivery. |
| `integration_legacy_deliveries` | Audit manually proved legacy delivery and its target, preserved evidence, and development journal identity. |
| `jobs` | Run bounded preset validation/Git publication with immutable inputs, resource class, nonce, deadlines, and result evidence. |
| `job_workspace_pins` | Prevent deletion/reuse of a workspace generation while an integration validation/publication job holds it. |
| `job_outbox` | Deduplicate durable job-completion facts that resume development publication. |

## Doctor checks

The registry below is authoritative for supported fixes. Report-only checks name an operator decision; a fix entry calls a guarded recovery path or rearms retry work.

| Check id | Run / supported fix | Purpose |
| --- | --- | --- |
| `integration.reviewed_file_guard` | `_check_reviewed_file_blocked_batches` / report only | Report batches still blocked by historical reviewed-file guard reservations. |
| `integration.delivery_path` | `_check_delivery_path` / report only | Name every project whose finished work has nowhere to go. |
| `integration.operational` | `_check_operational` / report only | Aggregate the reviewed read-only status command for every project. |
| `integration.orphaned_operations` | `_check_orphaned_operations` / report only | Report hierarchy operations that no configured runner can advance. |
| `integration.reused_task_identity` | `_check_reused_task_identity` / report only | Report live origins that predate the task now using their identity. |
| `integration.unreviewed_prs` | `_check_unreviewed_prs` / report only | Report recently completed tasks whose open PR has no review task. |
| `integration.branch_discards` | `_check_branch_discards` / `_fix_branch_discards` | Report branch discards parked in conflict, failed, or retry-exhausted state. |
| `integration.stranded_fences` | `_check_stranded_fences` / `_fix_stranded_fences` | Report owner rows whose former task writer no longer runs. |
| `integration.missing_canonical_owners` | `_check_missing_canonical_owners` / report only | Report train producers that a claim cannot attach to their own branch. |
| `integration.stranded_dependents` | `_check_stranded_dependents` / report only | Report development dependents held by legacy commits-less delivery ambiguity. |
| `integration.development_publisher_stalled` | `_check_publisher_stalled` / report only | Report durable publisher skips, stale work, and repeated validation infrastructure faults. |
| `integration.development_conflicts_unrepaired` | `_check_unrepaired_conflicts` / report only | Report parked completed sources no open repair chain still carries. |
| `integration.stranded_delegates` | `_check_stranded_delegates` / `_fix_stranded_delegates` | Report unfinished generated delegates whose owning operation already ended. |
| `integration.app_mode` | `_check_app_mode` / report only | Run `aq integration app-verify` for every App-mode project not disabled. |
| `integration.stale_repair_intents` | `_check_stale_repair_intents` / report only | Find live delegates whose stage or dossier still names a superseded intent. |
| `integration.missing_repair_owners` | `_check_missing_repair_owners` / report only | Report active detached repair delegates missing their exact branch reservation. |
| `integration.stale_schedule` | `_check_stale_schedule` / `_fix_stale_schedule` | Report outstanding requests that cannot end automatically or have ambiguous writes. |
| `integration.stuck_children` | `_check_stuck_children` / report only | Report completed children still unassembled into their collecting parent. |
| `integration.blocked_collectors` | `_check_blocked_collectors` / report only | Expose parents that the PAUSED-only collection scan cannot see. |
| `integration.finished_branch_owners` | `_check_finished_branch_owners` / `_fix_finished_branch_owners` | Report finished/gone owners eligible for release outside hierarchy/train authority. |

### [src.doctor.integration_checks](../../../src/doctor/integration_checks.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_operational_projection` | Keep the operator-relevant, non-secret part of `integration_status`. |
| `_check_operational` | Aggregate the reviewed read-only status command for every project. |
| `_check_app_mode` | Run `aq integration app-verify` for every App-mode project not disabled. |
| `_check_orphaned_operations` | Report hierarchy operations that no configured runner can advance. |
| `_review_dedup_key` | The dedup key `per-task-review` uses for its `ensure_task`. |
| `_pr_is_open` | `True` open, `False` merged/closed, `None` when it can't be told. |
| `_find_unreviewed` | Recently COMPLETED tasks that carry a PR but have no review task. |
| `_check_unreviewed_prs` | Report recently completed tasks whose open PR has no review task. |
| `_find_parked_discards` | Branch discards that stopped short of removing their ref. |
| `_check_branch_discards` | Report branch discards parked in conflict, failed, or retry-exhausted state. |
| `_fix_branch_discards` | Re-arm parked discards for one more pass. |
| `_check_missing_canonical_owners` | Report train producers that a claim cannot attach to their own branch. |
| `_find_stranded_fences` | Ownership rows still held for a task that has no writer left. |
| `_check_stranded_fences` | Report owner rows whose former task writer no longer runs. |
| `_fix_stranded_fences` | Run guarded recovery for every row currently diagnosed as stranded. |
| `_find_stranded_dependents` | Find candidates held behind a cleaned-up commits-less delivery. |
| `_check_stranded_dependents` | Report development dependents held by legacy commits-less delivery ambiguity. |
| `_find_publisher_stalls` | Durable symptoms of a development publisher that stopped making progress. |
| `_validation_infrastructure_stall` | A development validation that keeps failing to *finish*. |
| `_check_publisher_stalled` | Report durable publisher skips, stale work, and repeated validation infrastructure faults. |
| `_find_stranded_delegates` | Delegate tickets of an integration operation that has already ended. |
| `_check_stranded_delegates` | Report unfinished generated delegates whose owning operation already ended. |
| `_check_missing_repair_owners` | Report active detached repair delegates missing their exact branch reservation. |
| `_check_stale_repair_intents` | Find live delegates whose stage or dossier still names a superseded intent. |
| `_find_unrepaired_conflicts` | Completed sources parked on a merge conflict that no repair is carrying. |
| `_check_unrepaired_conflicts` | Report parked completed sources no open repair chain still carries. |
| `_fix_stranded_delegates` | Retire each stranded delegate and record the release. |
| `_find_stale_schedules` | Train schedules whose outstanding request nothing will end on its own. |
| `_stale_schedule_ok` | Return a clean stale-schedule diagnostic with no stranded request. |
| `_check_stale_schedule` | Report outstanding requests that cannot end automatically or have ambiguous writes. |
| `_fix_stale_schedule` | Free each `stale` request exactly as the scheduler's next pass would. |
| `_find_stuck_children` | COMPLETED children of collecting parents that were never assembled. |
| `_check_blocked_collectors` | Expose parents that the PAUSED-only collection scan cannot see. |
| `_check_stuck_children` | Report completed children still unassembled into their collecting parent. |
| `_stop_confirmer` | Resolve the daemon's provider-backed stopped-session probe. |
| `_describe_owner` | Render an owner row's branch, role, identity, status, and handoff state. |
| `_check_finished_branch_owners` | Report finished/gone owners eligible for release outside hierarchy/train authority. |
| `_fix_finished_branch_owners` | Release each owner row the check clears, re-proving it under lock. |
| `_check_reused_task_identity` | Report live origins that predate the task now using their identity. |
| `_check_delivery_path` | Name every project whose finished work has nowhere to go. |
| `_find_reviewed_file_blocked_batches` | Read active batches retaining historical reviewed-file-guard rejection evidence. |
| `_check_reviewed_file_blocked_batches` | Report batches still blocked by historical reviewed-file guard reservations. |
| `integration_checks` | Register the integration doctor checks and their supported guarded fixes. |
| `run_check` | Run one integration check directly against *db* (no registry needed). |

## Daemon, session, Git, and playbook runtime integration

These are the integration-bearing call sites in shared subsystems, not an inventory of every unrelated task/session/Git operation. Shared methods retain their wider duties. Nested initialization callbacks bind repository clients, root-intent reconciliation, candidate factories, and provider stop probes in `Orchestrator.initialize`.

### [src.commands.claim_commands](../../../src/commands/claim_commands.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_task_block` | The claimed task's own row, field-for-field with `task_show`'s core. |
| **`ClaimCommandsMixin`** | Mixed into CommandHandler. |
| `_cmd_task_claim` | Claim a ready task for the calling session (`aq task claim`). |
| `_attempt_claim_once` | Decide the outcome on one `immediate()` transaction, on *conn* only. |
| `_recover_stale_binding` | Unwind a claim binding whose task is no longer IN_PROGRESS. |
| `_prepare_and_activate_locked` | Reset the slot, write the claim file, activate. |

### [src.commands.git_commands](../../../src/commands/git_commands.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`GitCommandsMixin`** | Git PR and provenance migration commands for CommandHandler. |
| `_cmd_integration_migrate_provenance` | Authorize and run the exact legacy Git completion/replacement provenance inventory or migration. |
| `_cmd_pr_merge` | Merge a PR. |
| `_cmd_task_deliver` | Deliver a BLOCKED task's pushed branch into its default branch by hand. |
| `_check_ci_before_merge` | Apply `integration.merge_ci_policy` to a PR about to be merged. |

### [src.commands.job_commands](../../../src/commands/job_commands.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`JobCommandsMixin`** | Handle bounded job submission, including detached integration snapshots. |
| `_cmd_job_submit_integration` | Internal only: provision a detached snapshot and submit at band zero. |

### [src.commands.session_commands](../../../src/commands/session_commands.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`SessionCommandsMixin`** | Session command methods mixed into CommandHandler. |
| `_close_task_obsolete` | `aq task close <id> --obsolete --reason`: retire superseded work. |
| `_cmd_task_close` | Close a task with an outcome. |

### [src.commands.task_commands](../../../src/commands/task_commands.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_integration_cleanup_reason` | One resource a retired delegate still holds, named as its own explain reason. |
| `_recovery_incident_reason` | The open recovery incident with its owner, remaining budget and next action. |
| **`TaskCommandsMixin`** | Task command methods mixed into CommandHandler. |
| `_cmd_list_active_tasks_all_projects` | List active (non-terminal) tasks across ALL projects, grouped by project. |
| `_task_to_dict` | Serialize a `Task` to the standard dict used in list responses. |
| `_create_worker_filed_task` | Write a worker-filed task + its edges in one `immediate()` txn. |
| `_parent_key_mode_refusal` | Refuse `parent_key` in a hierarchy/train project, or `None`. |
| `_create_task` | File validated work through the hierarchy route when enabled, preserving origin and graph invariants. |
| `_cmd_create_task_graph` | Create a whole task graph in one transaction (supervisor-agent §8). |
| `_cmd_get_task` | Return task details including effective integration mode and delivery projection. |
| `_cmd_edit_task` | Apply authorized task edits while fencing hierarchy, route, and delivery identity changes. |
| `_cmd_task_recover` | Apply a reasoned task recovery decision after proving the exact provider-backed stop state. |
| `_cmd_restart_task` | Restart authorized work and restore its detached canonical branch reservation. |
| `_cmd_reopen_with_feedback` | Reopen a completed/failed task with feedback appended to its description. |
| `_cmd_archive_task` | Archive tasks — single task by ID or bulk by project. |
| `_cmd_explain_task` | Return the ordered list of reasons *task_id* isn't running. |
| `_cmd_project_ready` | Ready frontier for a project + withheld tasks with reasons. |
| `_cmd_ensure_task` | Find-or-create a task by (project_id, dedup_key). |

### [src.config](../../../src/config.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`GitHubAppConfig`** | Non-secret daemon GitHub App identity and private-key reference. |
| `validate` | Validate numeric App/installation ids and a safe private-key reference without exposing credentials. |
| **`ScratchProbeConfig`** | Non-secret identity for the operator-provisioned negative-control repo. |
| `validate` | Validate scratch repository/ref and configured protection/credential probe settings. |
| **Module functions** | Definitions in the linked module. |
| `validate_github_app_raw_config` | Reject inline or structurally ambiguous integration credentials. |
| **`IntegrationConfig`** | System-wide default integration policy. |
| `validate` | Validate delivery modes, CI policy, App settings, recovery sweeps, and publisher bounds. |
| **`AppConfig`** | Top-level application configuration aggregating all subsystem configs. |
| `validate` | Validate all configuration settings, delegating to per-section validators. |
| **Module functions** | Definitions in the linked module. |
| `load_config` | Load and validate application configuration from a YAML file. |

### [src.database.queries.archive_queries](../../../src/database/queries/archive_queries.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`ArchiveQueryMixin`** | Query mixin for archived task operations. |
| `set_delivery_observer` | Register the `~src.integration.delivery_observer.DeliveryObserver`. |
| `observe_removal_delivery` | Git evidence for the subtrees of *root_ids*, taken before any transaction. |
| `archive_task` | Archive *task_id* and its whole subtree atomically (spec §7). |
| `_development_integration_hold` | Say why development delivery still owns one of *ids*, or `None`. |
| `_archive_one` | Move a single task row from `tasks` into `archived_tasks`. |
| `archive_old_terminal_tasks` | Archive terminal tasks older than the threshold. |
| `_record_archive_refusal` | Remember why the sweep last refused *task_id*, if that has changed. |
| `list_archive_blocked_roots` | Eligible roots the sweep could not archive, and why. |
| `delete_archived_task` | Permanently delete an archived task. |
| `_row_to_archived_task` | Convert a database row from `archived_tasks` to a plain dict. |

### [src.database.queries.claim_queries](../../../src/database/queries/claim_queries.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_frontier_predicates` | Named acceptance predicates shared by claiming and diagnostics. |
| `_frontier_where` | The frontier predicate, for one project. |
| **`ClaimQueryMixin`** | Expects `self._engine` plus Task/Session/Workspace/Hierarchy mixins. |
| `select_ready_for_profile` | The §10 work query. |
| `take_task` | Fence + epoch bump + status write in **one** statement (spec §15). |
| `_release_claim_on` | Release only the exact pool claim, settle attempt bookkeeping, and preserve integration handoff resources. |
| `release_displaced_pool_claim` | Detach a draining pool session from a task it no longer owns. |
| `get_settled_pool_claim` | Prove a draining pool session's held task is terminal and truthfully settled. |
| `release_historical_pool_claim` | Release a stopped historical claim without touching its former holder. |

### [src.database.queries.hierarchy_queries](../../../src/database/queries/hierarchy_queries.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_settlement_clauses` | The §7 conditions both settlement legs share, correlated to `tasks`. |
| `stale_container_clauses` | The stale-status leg's graph half: a BLOCKED/PAUSED container that may settle. |
| `stale_delivery_children` | The children whose delivery git must prove before their stale container settles. |
| **`ProjectIntegrationMode`** | The two per-project constants the hierarchy claim predicates need. |
| `of` | Read the mode off a project row; `None` when there is no row. |
| **Module functions** | Definitions in the linked module. |
| `materialized_origin_when_hierarchical` | Require an exact origin or an active delegate reservation in enabled projects. |
| `_reserved_repair_branch` | A repair writes its operation's existing branch, not a new task origin. |
| `_reserved_verifier_branch` | A parent verifier checks the parent's branch, not a new task origin. |
| `integration_rework_cutoff` | The only task timestamp that invalidates an earlier delivery receipt. |
| `delivered_same_parent_prerequisites_when_hierarchical` | Require a direct sibling prerequisite to reach the shared parent first. |
| **`HierarchyQueryMixin`** | Expects `self._engine` plus BlockedStateMixin and TaskQueryMixin. |
| `guard_hierarchy_bulk_write` | Reject legacy bulk hierarchy writers for hierarchy-enabled projects. |
| `phase_mode_refusal` | The phase refusal for *project_id*, or `None` when phases are fine. |
| `hierarchy_prerequisite_delivery_head` | Return the latest exact parent head a sibling-dependent child needs. |
| `get_phase_hold_details` | Return additive, bounded strict-hold detail for declared phases. |
| `guard_integration_mutation` | Fence canonical hierarchy/lifecycle writers for enabled projects. |
| `set_parent` | Move *task_id* under *parent_id* (`None` = root). |
| `settle_containers` | Complete every seeded container whose children are all done (spec §7). |
| `stale_container_delivery_children` | Per stale candidate, the children git must prove delivered (no locks). |

### [src.database.queries.task_queries](../../../src/database/queries/task_queries.py)

| Scoped name | One-line purpose |
| --- | --- |
| `task_repository_id` | The `repo_id` a task of a project in *mode* is created with. |
| **`TaskQueryMixin`** | Query mixin for task operations. |
| `_insert_task_row` | Insert a single task row. |
| `update_task` | Update arbitrary task fields. |
| `_moved_task_repo_id` | The repository a task moved into *project_id* is delivered from. |
| `resume_task` | Remove only the explicit hold; keep approval and dependency state. |
| `recover_orphaned_pause` | Resume a task wedged in PAUSED with no timer and no operator hold. |
| `_resume_locked` | Restore a PAUSED task from its hold snapshot; the caller holds the row lock. |
| `_apply_transition` | Update task status with state-machine validation, on a caller-owned connection. |
| `transition_task` | Public status write: one transaction, then post-commit emission. |
| `delete_task` | Delete a task; with *cascade*, its whole subtree (spec §7). |
| `_delete_task_body` | The transactional body of `delete_task`, on a supplied `conn`. |
| `_delete_one` | Delete an active task and its FK references; archives retain comments and completion history. |
| `_row_to_task` | Convert a database row to a Task model. |

### [src.database.queries.task_references](../../../src/database/queries/task_references.py)

| Scoped name | One-line purpose |
| --- | --- |
| `find_integration_task_references` | Which of *ids* durable integration bookkeeping still names. |
| `find_integration_repository_references` | Return durable integration audit rows attached directly to repositories. |
| `describe_integration_references` | One line naming the tables (and tasks) that hold a subtree back. |

### [src.git.ci_gate](../../../src/git/ci_gate.py)

| Scoped name | One-line purpose |
| --- | --- |
| `parse_pr_url` | `https://github.com/o/r/pull/42` → `("o", "r", 42)`, else `None`. |
| **`BaseFreshness`** | Whether a PR head contains its base branch's tip. |
| `is_current` | Report whether the PR contains the observed base branch tip. |
| `summary` | Render the PR's current, stale, or unknown base-freshness verdict. |
| `as_dict` | Serialize the PR base ref, behind count, and freshness state. |
| **Module functions** | Definitions in the linked module. |
| `classify_base` | Judge `(base_ref, behind_by)` as `GitManager.apr_behind_base` returns it. |
| **`CiVerdict`** | What CI says about a PR head, and which checks said it. |
| `is_green` | Report whether the observed PR check rollup is green. |
| `summary` | One line naming the checks behind the verdict, for logs and errors. |
| **Module functions** | Definitions in the linked module. |
| `normalize_entry` | Reduce one rollup entry to `(check_name, STATE)`. |
| `_fold` | Collapse one check name's entries into a single state. |
| `classify_rollup` | Judge a PR's status-check rollup. |

### [src.git.github](../../../src/git/github.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`GitHubAccess`** | One startup-owned authentication service and shared `gh` runner. |
| `installation_token` | Return the selected Git transfer credential for one exact binding. |
| `bind_repository` | Resolve and verify one trusted repository through the shared runner. |
| **`GitHubExecutionAccess`** | Credential-aware execution seam consumed by `GitHubClient`. |
| `installation_token` | Define the selected Git-credential interface for one exact repository binding. |
| **`GitHubRunnerAccess`** | Compatibility access wrapper for staged client constructors. |
| `installation_token` | Bridge legacy runner-owned credentials until their removal. |
| **`GitHubClient`** | Validated repository operations shared by every credential source. |
| `installation_token` | Supply isolated Git with this client's startup-selected credential. |
| `exact_head_ref` | Read and validate one exact authenticated heads ref, returning absent only for a confirmed missing ref. |
| `exact_pull_request` | Read and validate the repository-bound PR's exact head, base, and state. |
| `has_comment_marker` | Read all PR comments and test for the exact idempotent cleanup marker. |
| `comment_pull_request` | Publish a repository-bound marker-bearing comment through the selected App client. |
| `close_pull_request` | Close the exact numbered PR through the selected App client. |
| `commit_check_runs` | Read all authenticated check runs for one exact valid commit. |
| `lookup_audit_pr` | Find the unique marker-bound audit PR and validate its head/base/repository identity. |
| `create_audit_pr` | Create/reconcile an exact candidate audit PR with a stable marker and immutable head/base identity. |
| `_audit_marker` | Validate the audit idempotency key and render its hidden PR body marker. |
| `_audit_pull_request` | Validate a returned PR's repository, head, base, state, and audit marker. |

### [src.git.manager](../../../src/git/manager.py)

| Scoped name | One-line purpose |
| --- | --- |
| `_delivery_source_rev` | The revision to `rev-parse` for a delivery *source*. |
| **`GitManager`** | Implementation/interface for the methods inventoried below. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `commit_identity_env` | `GIT_AUTHOR_*` / `GIT_COMMITTER_*` for *args*, or `{}`. |
| `bind_github_repository` | Resolve an authorized immutable GitHub repository binding through the shared GitHub access service. |
| `_resolve_delivery_tip` | Synchronous twin of `_aresolve_delivery_tip`. |
| `push_validated_delivery` | Synchronously resolve once, validate, and push an exact delivery OID. |
| `acommit_all` | Async version of `commit_all`. |
| `afetch_repository_oid` | Import an exact repository object with a credential selected for this fetch. |
| `apush_repository_oid` | Transfer an immutable OID under an exact lease with fresh credentials. |
| `_aresolve_delivery_tip` | Resolve a delivery *source* to the one commit id that will be pushed. |
| `apush_validated_ref` | Resolve *source_ref* once and push that exact commit to *branch*. |
| `_apush_oid` | Publish an immutable commit under an observed, exact remote lease. |
| `apush_validated_delivery` | Inspect and push one immutable delivery tip without a ref-name race. |
| `apush_expected_delivery` | Push one validated candidate with an exact remote old-tip lease. |
| `_apush_oid_with_app_auth_to_url` | Stage an exact graph, then push it without consulting worker Git state. |
| `_apr_delivery_diff` | NUL-delimited paths PR `identity` changes, derived from its pinned OIDs. |
| `apr_check_rollup` | Return the PR head check rollup; None remains an unknown verdict. |
| `apr_behind_base` | Return (base branch, behind count) for the authorized PR head. |
| `afind_open_pr` | Return an open or merged PR URL delivering *branch_name*, or `None`. |

### [src.jobs.adapters](../../../src/jobs/adapters.py)

| Scoped name | One-line purpose |
| --- | --- |
| `finite_command` | Translate existing command syntax to a server-owned finite preset. |
| **`PublisherJobs`** | Trusted publisher adapter. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `handler` | Resolve the injected trusted CommandHandler instance. |
| `submit` | Submit a finite detached integration validation job through CommandHandler. |
| `wait` | Reconcile and poll the durable validation job until its terminal result exists. |

### [src.models](../../../src/models.py)

| Scoped name | One-line purpose |
| --- | --- |
| `resolve_integration_mode_with_source` | Resolve the effective integration mode and where it came from. |
| `resolve_integration_mode` | Resolve the effective integration mode for a task (see above). |

### [src.orchestrator.core](../../../src/orchestrator/core.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`Orchestrator`** | Coordinates the full task lifecycle across multiple projects and agents. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `_observe_integration_source_ci` | Invoke the source-CI command under the integration-source-ci service principal. |
| `_repair_integration_source_ancestry` | Invoke exact source-ancestry repair under its scoped service principal. |
| `_dispatch_pending_integration_repairs` | Page interrupted repair handoffs and replay dispatch through CommandHandler as a service. |
| `_drain_branch_materializations` | Materialize reserved task refs through the fenced hierarchy service. |
| `_branch_materialization_hierarchy` | Resolve the command handler's exact hierarchy service for background materialization. |
| `_drain_branch_discards` | Advance branch discards an operator asked for when deleting a task. |
| `_sweep_stranded_owners` | Release quiet, provably abandoned branch owners every five minutes. |
| `stop_task` | Forcibly stop an in-progress task and release its agent. |
| `initialize` | Wire integration services, repository/App clients, ownership recovery, and all bounded tick handlers. |
| `shutdown` | Stop integration reconciliation and ready-owner recovery before releasing daemon infrastructure. |
| `run_one_cycle` | Drive the main cascade, including pre-scheduling materialization and delivery-aware settlement. |
| `_reconcile_sessions` | Tick session reconciliation and schedule the separate bounded ready-owner recovery pass. |

### [src.orchestrator.execution](../../../src/orchestrator/execution.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`ExecutionMixin`** | Agent execution pipeline methods mixed into Orchestrator. |
| `_launch_session_for_task_locked` | Start a session for *task* and return. |
| `_fail_session_launch` | Pause the task with a backoff after a failed session launch. |
| `_complete_session_task_locked` | Run the completion pipeline for a session-closed task. |
| `_restore_slot_before_handoff_proof` | Clean a failing writer's slot so its handoff proof can succeed. |
| `release_session_task_resources` | Serialize terminal cleanup under the task-control lock and run its integration-safe tail. |
| `_release_session_task_resources_locked` | Free everything a session-run task was holding. |

### [src.orchestrator.git_ops](../../../src/orchestrator/git_ops.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`GitOpsMixin`** | Git operations methods mixed into Orchestrator. |
| `_development_delivery_refusal` | Refuse a development-mode close, separating fixable from escalating. |
| `_phase_verify_aggregate_verifier` | Verify and complete a branchless parent aggregate exactly once. |
| `_phase_verify_hierarchy_producer` | Prove a managed hierarchy producer's owned delivery branch. |
| `_vault_only_delivery` | Recognize an explicit vault-only task with a usable vault attachment. |
| `_resolve_task_delivery` | Resolve work to exactly one of the assigned and checked-out refs. |
| `_effective_integration_mode` | Resolve the effective integration mode for *task*. |
| `_run_completion_phases` | The pipeline body. |
| `_phase_verify` | Pipeline phase: verify the agent left the workspace in the expected git state. |
| `_reserved_delivery_failure` | Return a fail-closed verification issue for a delivery diff. |
| `_phase_integrate` | Integration under the per-project merge slot. |

### [src.orchestrator.lifecycle](../../../src/orchestrator/lifecycle.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`LifecycleMixin`** | The lifecycle sweep, mixed into the Orchestrator. |
| `retry_obsolete_cleanup` | Retry the cleanup every obsolete close still owes (`aq task close --obsolete`). |

### [src.orchestrator.monitoring](../../../src/orchestrator/monitoring.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`MonitoringMixin`** | Monitoring and housekeeping methods mixed into Orchestrator. |
| `delivery_observer` | Git delivery truth for settlement and the readers outside the publisher. |

### [src.orchestrator.pools](../../../src/orchestrator/pools.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`PoolsMixin`** | Worker-pool sizing and convergence, mixed into `Orchestrator`. |
| `_delivery_admission` | Build the shared fresh Git-delivery admission snapshot for pool demand and claims. |
| `_measure_pools` | One `PoolMeasurement` for every pool profile, this tick. |
| `_release_container_claims` | Take back every pool claim on a container its holder did not fill. |
| `_terminate_pool_session_locked` | Stop the process before making its durable worker or workspace reusable. |

### [src.orchestrator.workspace](../../../src/orchestrator/workspace.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`WorkspaceMixin`** | Workspace management methods mixed into Orchestrator. |
| `_prepare_workspace_locked` | Acquire workspace(s) for the task and prepare the primary one. |
| `_hierarchy_origin_and_fence` | Resolve exact origin/target and the server-derived current role. |
| `_prepare_exact_origin_workspace` | Prepare any enabled checkout at its pinned origin under one owner fence. |
| `_prepare_slot_workspace` | Prepare an acquired slot worktree for *task*. |
| `aconfirm_integration_owner_handoff` | Stop and detach an attached integration writer before fencing it out. |
| `aconfirm_stopped_integration_pool_owner_handoff` | Recover one stale stopped pool writer without changing its requeued task. |
| `aconfirm_integration_pool_owner_handoff` | Detach a **pool** writer's checkout and release its exact attachment. |
| `aconfirm_integration_pool_published_repair_handoff` | Prove a qualified candidate repair without advancing its canonical ref. |
| `arecover_completed_integration_pool_claim` | Release one terminal pool holder only after proving its writer is gone. |
| `aconfirm_integration_owner_stopped_for_repair` | Stop one exact writer without touching its retained repair checkout. |
| `arelease_integration_writer_for_retry` | Prove an enabled writer stopped, then restore its task reservation. |
| `arelease_never_attached_integration_launch` | Release exact task resources after ownership won before attachment. |

### [src.orchestrator.workspace_attachments](../../../src/orchestrator/workspace_attachments.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`AcquisitionFailed`** | Raised when `acquire_for_task` cannot satisfy a required kind. |
| `__init__` | Configure the service’s collaborators and retained state. |
| **Module functions** | Definitions in the linked module. |
| `integration_handoff_release_is_confirmed` | Whether *owner* already durably released its exact old attachment. |
| `mark_integration_handoff_released` | Atomically record detach proof and release the exact old DB lock. |
| `recover_stopped_integration_pool_claim` | Release one historically stranded pool claim after an exact handoff. |
| `mark_integration_pool_handoff_released` | Record a **pool** writer's detach proof without unbinding its slot. |
| `mark_stopped_integration_pool_handoff_released` | Release an exact, stopped pool claim without replaying its task transition. |
| `release_never_attached_integration_launch` | Release a losing launch only when no integration writer was attached. |
| `detach_slot_for_integration_handoff` | Detach a clean, fully pushed slot without resetting its contents. |
| `detach_workspace_for_integration_handoff` | Prove and detach an exact pushed checkout before releasing its lock. |
| `effective_requirements` | The single load-bearing function that turns a task into requirements. |
| `acquire_for_task` | Acquire all required workspaces for a task. |
| `orphaned_integration_pool_handoff_is_recoverable` | Return the exact retired pool attachment that may be detached safely. |
| `orphaned_integration_pool_handoff_exclusion` | Hold an exact orphan's workspace exclusion through its Git handoff. |
| `_orphaned_integration_pool_handoff_is_recoverable_on` | Locked implementation of `orphaned_integration_pool_handoff_is_recoverable`. |
| `mark_orphaned_integration_pool_handoff_released` | CAS-release a recovered attachment after its checkout is detached. |
| `_mark_orphaned_integration_pool_handoff_released_on` | Record release while `orphaned_integration_pool_handoff_exclusion` is held. |

### [src.playbooks.runtime](../../../src/playbooks/runtime.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`_FrozenOperationRoute`** | Typed record including `activation_id`, `playbook_id`, `scope`, `scope_identifier`, `artifact_sha256`. |
| `from_target` | Freeze a dispatch target's playbook id, scope, artifact, and boundary identity. |
| `matches` | Compare a runtime destination to the operation's pinned route address. |
| **`V2PlaybookRuntime`** | Dispatch ready immutable activations and expose their timer triggers. |
| `__init__` | Configure the service’s collaborators and retained state. |
| `is_active` | Whether a healthy system activation currently owns this optional policy. |
| `_refresh_locked` | Reload ready activations, build integration destinations, and wake durable replay. |
| `accept_integration_event` | Persist matching destinations, then replay them asynchronously. |
| `_select_integration_destinations` | Select current consumers while pinning the operation's owner. |
| `_ensure_integration_reconciler` | Start one background replay loop for pending durable integration destinations. |
| `_schedule_integration_pending` | Fill bounded replay slots without waiting for slow sibling jobs. |
| `_integration_done` | Wake the durable replay loop when one integration dispatch task finishes. |
| `_reconcile_integration_pending` | Page accepted pending integration jobs and fill bounded replay slots. |
| `_wait_for_integration_wakeup` | Wait for a replay completion or finite polling interval. |
| `_dispatch_integration_pending` | Dispatch one protected row; every failure leaves it retryable. |
| `shutdown` | Unsubscribe event handling and cancel/await integration replay tasks. |

### [src.playbooks.services](../../../src/playbooks/services.py)

| Scoped name | One-line purpose |
| --- | --- |
| `is_integration_route_event` | Test membership in the integration lifecycle event set requiring route resolution. |
| `resolve_integration_route` | Resolve project authority for one integration-lifecycle dispatch. |
| **`DatabaseActivationSource`** | Project Package 3 activation rows into the engine's tiny read contract. |
| `ready_activations` | Select authorized ready activations, enforce frozen integration routes, and suppress development reviews. |
| `development_reviews_suppressed` | Read whether development mode suppresses legacy review pipelines for the project. |
| `legacy_final_review_suppressed` | Combine development and configured legacy final-review suppression policy. |

### [src.prime.sections](../../../src/prime/sections.py)

| Scoped name | One-line purpose |
| --- | --- |
| `build_task_section` | Task id/title/status/description (design §5.2 #3). |
| `build_integration_delivery_summary` | Render the same receipt projection that gates parent verification. |
| `build_task_context_section` | `task_context` rows incl. inlined `spec_ref` + attachments (design §5.2 #4). |

### [src.sessions.reconciler](../../../src/sessions/reconciler.py)

| Scoped name | One-line purpose |
| --- | --- |
| **`SessionReconciler`** | The cascade step that owns session lifecycle. |
| `_stop_drain_acked_holder` | Stop a drain-acked pool worker whose held task no close can change. |
| `_stop_retired_delegate_writer` | Stop a drain-acked pool worker whose held delegate can never close. |
| `_stop_settled_claim_holder` | Stop a drain-acked pool worker still bound to a task that already closed. |
| `_claim_starting_reconciliation` | Fence an enabled stale STARTING row, or defer a concurrent launch. |
| `_apply_provider_failover` | A mid-task death its provider explains (provider-failover D13). |
| `_step_orphans` | Reconcile the two ways a session row and its task can disagree. |
| `_release_task` | Free the agent and the workspace lock a terminal task was holding. |

## Shipped playbook rules

The following rows are read from the reviewed **V2** artifacts, not inferred from headings. Their human-authored integration sources are shipped disabled; the sentinel and default router are shipped enabled. Installation imports the reviewed bundles and does not overwrite installed vault copies, so these rows establish shipped policy, not which policy an operator currently bound. `schema_version: 2` is the artifact format; a playbook’s `version: 1` is its content revision.

Install sources: [src/vault.py](../../../src/vault.py); durable dispatch: [src/integration/outbox.py](../../../src/integration/outbox.py) and [src/playbooks/runtime.py](../../../src/playbooks/runtime.py).

### `parent-integration`

Authored source: [src/prompts/integration_playbooks/parent-integration.md](../../../src/prompts/integration_playbooks/parent-integration.md). Installed reviewed bundle: [Reviewed source](../../../src/prompts/reviewed_playbooks/parent-integration/source.md), [Artifact](../../../src/prompts/reviewed_playbooks/parent-integration/artifact.json), [Manifest](../../../src/prompts/reviewed_playbooks/parent-integration/manifest.md).

| Rule | Trigger | One-line purpose |
| --- | --- | --- |
| `parent-integration.completed-child-readiness` | `task.completed` | Recheck parent readiness after a child passes and ask for a human failed-child decision when policy requires it. |
| `parent-integration.failed-child-readiness` | `task.failed` | Recheck parent readiness after child failure and create the reasoned disposition gate when required. |
| `parent-integration.file-children` | `task.child_added` | File newly added children through the hierarchy command. |
| `parent-integration.checkpoint-parent` | `task.parent_checkpointed` | Apply the exact parent checkpoint transition after its durable event. |
| `parent-integration.promote-delivery` | `delivery.ready` | Promote one reviewed child; on conflict start and dispatch the existing bounded primary repair stage. |
| `parent-integration.project-delivery-readiness` | `delivery.applied` | Recheck parent readiness after a child receipt is applied, including required failed-child decisions. |
| `parent-integration.wake-parent-verifier` | `task.integration_ready` | Transfer the exact collection fence to the parent verifier after integration becomes ready. |
| `parent-integration.record-repair-result` | `integration.ci_completed`; `{"conclusion": "failure"}` | Count a failed exact CI attempt against its current repair stage. |
| `parent-integration.verify-parent` | `integration.ci_completed`; `{"conclusion": "success", "target_kind": "parent"}` | Verify the exact parent generation/head after successful authenticated parent CI. |
| `parent-integration.dispatch-debug` | `integration.repair_exhausted` | Dispatch the named operation’s bounded debug stage after primary exhaustion. |
| `parent-integration.expire-repair-stage` | `integration.repair_deadline_due` | Expire the exact bounded stage from its deadline event. |
| `parent-integration.reconcile-resolution-push` | `integration.resolution_push_observed` | Reconcile the exact promotion intent after a resolution push is observed. |
| `parent-integration.complete-verified-parent` | `task.integration_verified` | Complete the parent only against the exact operation/verification evidence. |
| `parent-integration.observe-repair-close` | `integration.repair_delegate_closed` | Consume delegate-close lifecycle information without treating close as successful repair evidence. |

| Command step | Command |
| --- | --- |
| `parent-integration.checkpoint-parent--checkpoint` | `integration_checkpoint_parent` |
| `parent-integration.complete-verified-parent--complete` | `integration_complete_parent` |
| `parent-integration.completed-child-readiness--ask-human` | `gate_create` |
| `parent-integration.completed-child-readiness--readiness` | `integration_delivery_readiness` |
| `parent-integration.dispatch-debug--dispatch` | `integration_repair_dispatch` |
| `parent-integration.expire-repair-stage--expire` | `integration_repair_timeout` |
| `parent-integration.failed-child-readiness--ask-human` | `gate_create` |
| `parent-integration.failed-child-readiness--readiness` | `integration_delivery_readiness` |
| `parent-integration.file-children--file` | `integration_file_children` |
| `parent-integration.project-delivery-readiness--ask-human` | `gate_create` |
| `parent-integration.project-delivery-readiness--readiness` | `integration_delivery_readiness` |
| `parent-integration.promote-delivery--dispatch-primary` | `integration_repair_dispatch` |
| `parent-integration.promote-delivery--promote` | `delivery_promote` |
| `parent-integration.promote-delivery--start-repair` | `integration_repair_start` |
| `parent-integration.reconcile-resolution-push--reconcile` | `integration_reconcile_promotion` |
| `parent-integration.record-repair-result--record` | `integration_record_repair` |
| `parent-integration.verify-parent--verify` | `integration_parent_verify` |
| `parent-integration.wake-parent-verifier--transfer` | `integration_transfer_owner` |

### `root-train`

Authored source: [src/prompts/integration_playbooks/root-train.md](../../../src/prompts/integration_playbooks/root-train.md). Installed reviewed bundle: [Reviewed source](../../../src/prompts/reviewed_playbooks/root-train/source.md), [Artifact](../../../src/prompts/reviewed_playbooks/root-train/artifact.json), [Manifest](../../../src/prompts/reviewed_playbooks/root-train/manifest.md).

| Rule | Trigger | One-line purpose |
| --- | --- | --- |
| `root-train.seal-due-frontier` | `integration.sweep_due` | Seal the current reviewed root frontier and release the schedule request if the train is empty. |
| `root-train.construct-and-test` | `integration.sealed` | Construct the sealed candidate, observe exact CI, and dispatch bounded repair on merge conflict. |
| `root-train.promote-green-candidate` | `integration.candidate_green` | Promote the exact attested candidate; rebuild and retest if its main base moved. |
| `root-train.repair-red-candidate` | `integration.candidate_red` | Dispatch the exact candidate operation’s current repair stage after red evidence. |
| `root-train.continue-closed-root-repair` | `integration.repair_delegate_closed` | Resolve the current delegate-close identity, then build and retest its exact candidate without inferring success from close. |
| `root-train.dispatch-debug` | `integration.repair_exhausted` | Dispatch the operation’s server-current stage under its frozen repair policy and authority. |
| `root-train.release-promoted` | `integration.batch_promoted` | Release only the exact terminal main-delivery request, independently of pending cleanup. |
| `root-train.cleanup-promoted` | `integration.cleanup_requested` | Run the promoted batch’s journaled cleanup through its guarded command. |

| Command step | Command |
| --- | --- |
| `root-train.cleanup-promoted--run` | `integration_cleanup` |
| `root-train.construct-and-test--build` | `integration_build_candidate` |
| `root-train.construct-and-test--ci` | `integration_ci_evidence` |
| `root-train.construct-and-test--dispatch` | `integration_repair_dispatch` |
| `root-train.continue-closed-root-repair--build` | `integration_build_candidate` |
| `root-train.continue-closed-root-repair--ci` | `integration_ci_evidence` |
| `root-train.continue-closed-root-repair--current` | `integration_repair_close_current` |
| `root-train.continue-closed-root-repair--dispatch` | `integration_repair_dispatch` |
| `root-train.dispatch-debug--dispatch` | `integration_repair_dispatch` |
| `root-train.promote-green-candidate--ci` | `integration_ci_evidence` |
| `root-train.promote-green-candidate--dispatch` | `integration_repair_dispatch` |
| `root-train.promote-green-candidate--promote` | `integration_promote_main` |
| `root-train.promote-green-candidate--rebuild` | `integration_build_candidate` |
| `root-train.release-promoted--run` | `integration_release` |
| `root-train.repair-red-candidate--dispatch` | `integration_repair_dispatch` |
| `root-train.seal-due-frontier--release-empty` | `integration_release` |
| `root-train.seal-due-frontier--seal` | `integration_seal` |

### `agent-queue-parent-integration`

Authored source: [src/prompts/project_playbooks/agent-queue/agent-queue-parent-integration.md](../../../src/prompts/project_playbooks/agent-queue/agent-queue-parent-integration.md). Installed reviewed bundle: [Reviewed source](../../../src/prompts/reviewed_playbooks/agent-queue-parent-integration/source.md), [Artifact](../../../src/prompts/reviewed_playbooks/agent-queue-parent-integration/artifact.json), [Manifest](../../../src/prompts/reviewed_playbooks/agent-queue-parent-integration/manifest.md).


The rule and command-step names are the same as `parent-integration` above; qualify every row with `agent-queue-parent-integration` for this bundle. Its normalized executable graph is identical.

### `agent-queue-root-train`

Authored source: [src/prompts/project_playbooks/agent-queue/agent-queue-root-train.md](../../../src/prompts/project_playbooks/agent-queue/agent-queue-root-train.md). Installed reviewed bundle: [Reviewed source](../../../src/prompts/reviewed_playbooks/agent-queue-root-train/source.md), [Artifact](../../../src/prompts/reviewed_playbooks/agent-queue-root-train/artifact.json), [Manifest](../../../src/prompts/reviewed_playbooks/agent-queue-root-train/manifest.md).


The rule and command-step names are the same as `root-train` above; qualify every row with `agent-queue-root-train` for this bundle. Its normalized executable graph is identical.

### `ci-main-sentinel`

Authored source: [src/prompts/project_playbooks/agent-queue/ci-main-sentinel.md](../../../src/prompts/project_playbooks/agent-queue/ci-main-sentinel.md). Installed reviewed bundle: [Reviewed source](../../../src/prompts/reviewed_playbooks/ci-main-sentinel/source.md), [Artifact](../../../src/prompts/reviewed_playbooks/ci-main-sentinel/artifact.json), [Manifest](../../../src/prompts/reviewed_playbooks/ci-main-sentinel/manifest.md).

| Rule | Trigger | One-line purpose |
| --- | --- | --- |
| `ci-main-sentinel.keep-main-green` | `timer.15m` | Observe default-branch CI; reuse/file a failure-owned root repair, record coverage, or escalate after the finite repair allowance. |

| Command step | Command |
| --- | --- |
| `ci-main-sentinel.keep-main-green--ensure_repair_task` | `ensure_task` |
| `ci-main-sentinel.keep-main-green--escalate_to_human` | `escalation_create` |
| `ci-main-sentinel.keep-main-green--read_baseline` | `ci_baseline_status` |
| `ci-main-sentinel.keep-main-green--record_repair` | `ci_repair_adopt` |

### `default-assignment-routing`

Authored source: [src/prompts/default_playbooks/default-assignment-routing.md](../../../src/prompts/default_playbooks/default-assignment-routing.md). Installed reviewed bundle: [Reviewed source](../../../src/prompts/reviewed_playbooks/default-assignment-routing/source.md), [Artifact](../../../src/prompts/reviewed_playbooks/default-assignment-routing/artifact.json), [Manifest](../../../src/prompts/reviewed_playbooks/default-assignment-routing/manifest.md).

| Rule | Trigger | One-line purpose |
| --- | --- | --- |
| `default-assignment-routing.route-task` | `task.route_needed` | Plan and apply one current worker route; integration/development repair origin policy constrains the lane, and missing classification is the only LLM step. |

| Command step | Command |
| --- | --- |
| `default-assignment-routing.route-task--apply_a` | `task_route_apply` |
| `default-assignment-routing.route-task--apply_b` | `task_route_apply` |
| `default-assignment-routing.route-task--apply_c` | `task_route_apply` |
| `default-assignment-routing.route-task--plan_classified` | `task_route_plan` |
| `default-assignment-routing.route-task--plan_first` | `task_route_plan` |
| `default-assignment-routing.route-task--plan_unclassified` | `task_route_plan` |

Its classification step calls the compiler profile only when route planning returns `needs_classification`; this is policy classification, not a scheduling-time LLM call inside integration services.

Both shared/project parent graphs and shared/project root graphs are equivalent after removing source-location metadata. The authored root prose differs at exhausted-repair continuation, but both compiled dispatch steps supply only `operation_id`; the command handler resolves the current stage, and the frozen `RepairPolicy.on_exhausted` determines continuation. Treat the prose difference as documentation drift, not as a separate executable repair strategy. CI main sentinel repairs still require the project’s normal review and train collection.


## Overlap and simplification candidates

“Confirmed” means the cited implementations or artifacts establish the relationship at this snapshot. “Suspected” marks a potential common helper or removable path whose behavioral equivalence still needs a dedicated design and regression evidence. A merge recommendation is future work, not permission to collapse guards, replace Git proof with task status, or extend a frozen budget.

| Evidence | Concrete pair or group | One-line recommendation |
| --- | --- | --- |
| Confirmed: AST bodies identical | [`_invoke_adapter`](../../../src/commands/contracts/integration.py#L2362) + [`_hierarchy_adapter`](../../../src/commands/contracts/integration.py#L2576) | **DELETE** one adapter and route both contract families through the same typed outcome/value conversion. |
| Confirmed: normalized rule/step graphs identical | [shared parent bundle](../../../src/prompts/reviewed_playbooks/parent-integration/artifact.json) + [project parent bundle](../../../src/prompts/reviewed_playbooks/agent-queue-parent-integration/artifact.json) | **MERGE** their authored rule body through one build-time template; keep separate scope/activation identities and reviewed bundles. |
| Confirmed: normalized graphs identical; authored prose differs | [shared root source](../../../src/prompts/integration_playbooks/root-train.md) + [project root source](../../../src/prompts/project_playbooks/agent-queue/agent-queue-root-train.md) + [`RepairPolicy`](../../../src/integration/models.py#L82) | **MERGE** the repeated executable policy source and correct exhausted-stage prose to name the frozen `on_exhausted` setting; keep scope identities. |
| Confirmed: shared service already serializes both entries | [`Orchestrator.run_one_cycle`](../../../src/orchestrator/core.py#L2762) + [`IntegrationService._reconcile`](../../../src/integration/service.py#L107) → [`BranchMaterializationService`](../../../src/integration/branch_materialization.py#L51) | **KEEP** both entry points and their one shared drain lock; they serve pre-assignment latency and remote reconciliation fairness. |
| Confirmed: different eligibility and authority | [`OwnerRecovery.recover`](../../../src/integration/owner_recovery.py#L183) + [`reconcile_ready_integration_owners`](../../../src/integration/completion_recovery.py#L55) + [`StaleOwnerRelease`](../../../src/integration/stale_owners.py#L123) | **KEEP** the selectors; factor common stopped-writer/ref proof and fenced-release transaction steps without broadening release eligibility. |
| Confirmed: different ownership scope | [`release_finished_branch_owners`](../../../src/integration/finished_owners.py#L122) + [`terminal_reservation_clause`](../../../src/integration/drain_owners.py#L25) + [`StaleOwnerRelease`](../../../src/integration/stale_owners.py#L123) | **KEEP** finished ordinary-task release, terminal drain classification, and integration stale-owner release as distinct paths; share exact-head/provider-stop observations where valid. |
| Confirmed: one shared delegate retirement path already exists | [`release_delegates_on`](../../../src/integration/delegate_release.py#L250) + [`RepairService.retire_terminal_delegates`](../../../src/integration/repair.py#L143) + [`IntegrationRecoveryControls`](../../../src/integration/recovery_controls.py#L41) | **KEEP** `release_delegates_on` as the single mutation path and use it from recovery, cancellation, and doctor; delete any future parallel retirement implementation. |
| Confirmed: different cancellation policy | [`IntegrationRecoveryControls.abort`](../../../src/integration/recovery_controls.py#L560) + [`DevelopmentIntegration.cancel_preserving`](../../../src/integration/development.py#L3380) | **KEEP** the separate abort versus preserve-for-delivery outcomes; merge only shared terminal delegate release and stopped-owner recovery steps. |
| Confirmed: shared classifier/release already reused | [`classify_outstanding_request_on`](../../../src/integration/stale_schedule.py#L129) + [`release_stale_request`](../../../src/integration/stale_schedule.py#L476) + [`IntegrationControlService.clear_stale_request`](../../../src/integration/controls.py#L130) | **KEEP** one stale-request classifier and one release transaction; operator, doctor, and scheduler wrappers should remain delegations. |
| Confirmed: distinct resolution identities; equivalence of common checks suspected | [`RepairRebind`](../../../src/integration/repair_rebind.py#L20) + [`DetachedRepairRebind`](../../../src/integration/detached_repair_rebind.py#L47) + [`PreservedRepairRecovery`](../../../src/integration/preserved_repair.py#L39) | **MERGE** reusable current-scope/lineage diagnostics and result formatting; keep separate live, detached-unpublished, and preserved-ref proof/mutation paths. |
| Confirmed: separate repair/recovery transitions | [`IntegrationRecoveryControls.resume`](../../../src/integration/recovery_controls.py#L55) + [`PromotionService.recover_unwritten_resolution`](../../../src/integration/promotion.py#L469) + [`CollectingParentRecovery`](../../../src/integration/collecting_parent_recovery.py#L27) | **KEEP** explicit rearm, unwritten-intent successor, and same-episode collector recovery; expose their diagnosis through one operator recovery facade. |
| Confirmed: different producer/delegate reservations | [`reserve_canonical_task_branch`](../../../src/integration/canonical_reservation.py#L21) + [`RepairService.reserve_delegate`](../../../src/integration/repair.py#L1096) | **KEEP** stopped canonical producer reservation separate from active operation-owned repair dispatch; reuse guarded fence/CAS primitives. |
| Confirmed: similar normalization, different null/facts handling | [`_blocker`](../../../src/integration/controls.py#L77) + [`_sorted_blockers`](../../../src/integration/controls.py#L81) + [`_blocker`](../../../src/integration/status.py#L56) + [`_sorted_blockers`](../../../src/integration/status.py#L62) | **MERGE** blocker construction/sorting into a shared representation after preserving extra status facts and the control digest’s exact stable bytes. |
| Confirmed: parallel evidence accounting with different dedup storage | [`IntegrationAttestationService._enqueue_candidate_result`](../../../src/integration/attestation.py#L605) + [`RepairService.record_result`](../../../src/integration/repair.py#L787) | **MERGE** the pure evidence/dossier/attempt fold and explicitly reconcile dossier dedup versus stage-evidence dedup; keep each caller’s lock and continuation authority. |
| Confirmed: multiple idempotent wakeups; removable equivalence suspected | [`GreenPromotionReconciler`](../../../src/integration/green_continuation.py#L149) + [`IntegrationAttestationService._enqueue_candidate_result`](../../../src/integration/attestation.py#L605) + [`CandidateService._schedule_construction_retry`](../../../src/integration/candidates.py#L661) | **KEEP** crash-recovery wakeups until one shared current-subject continuation decision proves all retry paths covered; then remove redundant decision code. |
| Confirmed: common generated-file policy; exact runner equivalence suspected | [`regenerated_tree`](../../../src/integration/regeneration.py#L71) + [`DevelopmentIntegration.merge_member`](../../../src/integration/development.py#L550) | **MERGE** generated-path detection and bounded regeneration process plumbing; keep scratch candidate construction versus journaled publisher checkout strategies. |
| Confirmed: repeated retained-store/Git wrappers; exact semantics suspected | [`PromotionService`](../../../src/integration/promotion.py#L85) + [`CandidateService`](../../../src/integration/candidates.py#L243) + [`RootPromotionService`](../../../src/integration/main_promotion.py#L134) + [`OwnerRecovery`](../../../src/integration/owner_recovery.py#L160) | **MERGE** low-level retained-store, object/tree, and ancestry helpers after matching error and timeout contracts; keep authority and ref-write journals in their owning services. |
| Confirmed: independent trust boundaries | [`AuthenticatedGitHubObserver`](../../../src/integration/ci.py#L903) + [hosted attestation verifier](../../../src/integration/hosted_attestation.py) + [legacy PR CI gate](../../../src/git/ci_gate.py) | **KEEP** authenticated train evidence, standalone hosted verification, and generic PR rollup gating separate; share test vectors rather than weakening proof requirements. |
| Confirmed: different delivery contracts | [`DeliveryObserver`](../../../src/integration/delivery_observer.py#L290) + [`DeliverySnapshot.evaluate`](../../../src/integration/delivery_truth.py#L174) + [`AdmissionSnapshot`](../../../src/integration/admission.py#L279) | **KEEP** Git-observed development delivery distinct from exact train receipts/attestation; share immutable source and ancestry observations only. |
| Confirmed: distinct cleanup authorization; reusable action suspected | [`IntegrationCleanupService._cleanup_pr`](../../../src/integration/cleanup.py#L204) + [`DeliveredPullRequestClosure.run`](../../../src/integration/pr_delivery.py#L56) | **MERGE** reusable marker/comment/close mechanics only if exact-head rechecks and irreversible journal rules survive; keep frozen batch proof versus independent Git delivery proof. |
| Confirmed: different retention and deletion policies | [`IntegrationCleanupService`](../../../src/integration/cleanup.py#L59) + [`BranchDiscardService`](../../../src/integration/branch_discard.py#L135) + [delivered-branch collection](../../../src/integration/delivery_branches.py) | **KEEP** promoted-batch cleanup, explicit obsolete-branch discard, and delivered task-branch retention as separate policies; reuse existing backup and held-ref guards. |
| Confirmed: shared exact PR creation purpose, different recovery episode | [`RootPullRequestReconciler`](../../../src/integration/root_pull_requests.py#L138) + [`RootDeliveryRedrive`](../../../src/integration/root_pull_requests.py#L215) + [`recover_completed_pr_links`](../../../src/integration/completion_recovery.py#L380) | **KEEP** one root PR opener and separate current-state recovery selectors; merge missing-link diagnosis/observation where the same proof applies. |
| Confirmed: different provenance mutations | [`ReusedIdentityRebind`](../../../src/integration/identity_rebind.py#L117) + [`ProvenanceMigration`](../../../src/integration/provenance_migration.py#L53) + [`DevelopmentIntegration.settle_parked`](../../../src/integration/development.py#L2435) | **KEEP** canonical-origin retirement, frozen source-provenance rebind, and reasoned parked-manifest settlement separate; share dry-run/audit presentation. |
| Confirmed: separate CI subjects and incident policy | [`CiCommandsMixin._cmd_ci_baseline_status`](../../../src/commands/ci_commands.py#L386) + [`CandidateCIService.handle`](../../../src/integration/candidate_ci.py#L14) + [`ParentCIService.handle`](../../../src/integration/parent_ci.py#L100) | **KEEP** main-health incident repair, root candidate CI, and parent verification distinct; consolidate result classification only where exact subject identity survives. |
