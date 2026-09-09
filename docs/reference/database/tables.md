# Table families

All 98 tables, grouped by what they are for. Each family opens with what it is
*about*; the tables inside it are listed with their purpose, the columns that
carry meaning, and any invariant the database itself enforces.

The definitive definition of every table is
[`src/database/tables.py`](../../../src/database/tables.py) — this page names
what the columns mean, not what type they are. For the concepts behind them,
start at [the database overview](README.md).

> **Reading the invariant column.** *check* is a `CheckConstraint` — the row is
> rejected outright. *trigger* is a PL/pgSQL guard from
> [`migrations/integration_guards.py`](../../../migrations/integration_guards.py)
> — the statement is rejected with a message. *partial unique* is a unique index
> with a `WHERE` clause, which is how "at most one *live* row" is expressed.
> *soft ref* means the column holds another row's id with no foreign key, so the
> row survives its subject's deletion.

## Contents

| Family | Tables | About |
|---|---|---|
| [Projects and installation](#projects-and-installation) | 5 | The scheduling unit and installation-wide settings. |
| [Tasks and the work graph](#tasks-and-the-work-graph) | 16 | Work, its shape, its history and its archive. |
| [Graph layout](#graph-layout) | 5 | The precomputed geometry the dashboard's graph tab renders. |
| [Agents, profiles and sessions](#agents-profiles-and-sessions) | 8 | Who runs work, in what process, with what identity. |
| [Workspaces](#workspaces) | 4 | The directories work happens in, and their locks. |
| [Messaging and human decisions](#messaging-and-human-decisions) | 8 | Inboxes, escalations, the hourly digest. |
| [Playbooks and workflows](#playbooks-and-workflows) | 7 | Compiled automation and its durable run state. |
| [Accounting and telemetry](#accounting-and-telemetry) | 4 | Spend, events, fleet metrics, provider quota. |
| [Plugins and onboarding](#plugins-and-onboarding) | 3 | Installed plugins and the project-creation saga. |
| [Integration and delivery](#integration-and-delivery) | 38 | Getting reviewed work onto a branch exactly once. |

## Projects and installation

A **project** is the unit of scheduling, budget and repository ownership.
Everything else that is not installation-wide hangs off one.

| Table | Purpose | Key columns | Invariants |
|---|---|---|---|
| `projects` | One row per project: its name, its scheduling weight and caps, its repository, and its integration policy. | `status`, `max_concurrent_agents`, `credit_weight`, `budget_limit`, `repo_url`, `integration_mode`, `hierarchical_integration_mode`/`_desired_mode`/`_generation` | check on both integration mode columns (`disabled`/`observe`/`hierarchy`/`train`/`development`); generation ≥ 0 |
| `repos` | Legacy per-project repository record. Superseded by the `repo_*` columns on `projects`, which a startup migration backfills from the first row here. | `url`, `default_branch`, `checkout_base_path`, `source_type` | — |
| `project_constraints` | A temporary scheduling hold on one project: exclusive mode, a per-type agent cap, or a hard pause. | `exclusive`, `max_agents_by_type`, `pause_scheduling` | one row per project |
| `system_config` | Installation-wide key/value settings that belong in the database rather than `config.yaml`. | `key`, `value` | — |
| `rate_limits` | Per-agent-type token buckets. | `agent_type`, `limit_type`, `max_tokens`, `current_tokens`, `window_start` | — |

## Tasks and the work graph

A **task** is the unit of work. The graph around it is typed edges plus gates:
an edge says "this task waits for that one", a gate says "this task waits for a
human, a timer or an external event".

| Table | Purpose | Key columns | Invariants |
|---|---|---|---|
| `tasks` | The work item: title, description, status, priority, routing, claim epoch and the derived blocked flag. 39 columns, the widest table in the schema. | `status`, `priority`, `is_blocked`, `parent_task_id`, `profile_id`, `claim_epoch`, `dedup_key`, `next_child_ordinal`, `created_by_kind`/`_id` | `is_blocked` is a projection, never authored; `parent_task_id` is a cache of the `parent-child` edge |
| `task_dependencies` | Every edge in the graph, typed. Only `blocks`, `parent-child`, `waits-for` and `conditional-blocks` gate readiness; `discovered-from`, `related`, `duplicates` and `supersedes` are provenance. | `task_id`, `depends_on_task_id`, `dep_type`, `description` | check rejects self-edges and unknown types; **partial unique** `uq_task_deps_single_parent` allows exactly one `parent-child` edge per task |
| `gates` | One blocking condition: a human decision, a timer, a PR merge, a CI run, an event, another task, or routing. | `gate_type`, `question`, `await_id`, `status`, `resolved_by`, `resolution`, `timeout_at` | check on type and status; **partial unique** on `(project_id, gate_type, await_id)` for open gates only, which is what makes gate creation idempotent |
| `task_gates` | Which tasks are waiting on which gate. Deleting the last waiter expires the gate. | `task_id`, `gate_id` | FK is `NO ACTION`: a task cannot be deleted while it waits on a gate |
| `task_criteria` | Acceptance criteria attached to a task, in display order. | `type`, `content`, `sort_order` | — |
| `task_context` | Extra input material handed to the agent — files, specs, prior output. | `type`, `label`, `content` | — |
| `task_metadata` | Arbitrary JSON-encoded key/value state on a task, including the `container` flag and pause snapshots. | `key`, `value` | — |
| `task_labels` | Free-form labels for filtering. | `task_id`, `label` | — |
| `task_tools` | Per-task tool configuration. | `type`, `config` | — |
| `task_comments` | The task's conversation: notes from agents, humans and the supervisor. `kind='progress'` is the author asserting that work actually advanced — the only kind the digest may treat as progress. | `body`, `author_kind`, `author_id`, `kind` | checks on `author_kind`, `kind`, and body length 1–16000; `task_id` is a **soft ref** so comments span active *and* archived tasks |
| `task_results` | Per-attempt agent output: summary, files changed, error, tokens. Retry history, one row per attempt. | `result`, `summary`, `files_changed`, `error_message` | — |
| `task_completion_records` | The append-only receipt of one accepted `task_close`: outcome, failure class, tests and commands run, branch, commits, PR, deliverables. | `outcome`, `work_outcome`, `failure_class`, `tests`, `commands`, `deliverables` | never updated; `task_id` is a **soft ref** so the receipt survives archive |
| `task_assignment_routes` | The current successful routing decision for a task — which intelligence class and provider a playbook chose, and why. | `intelligence_class`, `provider`, `playbook_id`, `input_hash`, `reason` | one row per task; `task_id` FK is `CASCADE` |
| `task_proposals` | A staged batch of tasks and edges awaiting human approval before it enters the live graph. `payload` is `{"tasks": [...], "edges": [...]}`. | `source`, `payload`, `status` | check: `draft`/`ready`/`committed`/`discarded` |
| `archived_tasks` | Where a task row goes when its subtree is archived. Same shape as `tasks` minus the live-execution columns, plus `archived_at`. | `archived_at`, `status` | see [Data lifecycle](data-lifecycle.md) |
| `hierarchy_migration_rejects` | Audit of tasks the hierarchy canonicalisation could not place, with the reason (`cross_project`, `cycle`, `depth`, `not_found`, `duplicate`). | `run_id`, `task_id`, `parent_id`, `source`, `reason` | written by [`hierarchy_migration.py`](../../../src/database/hierarchy_migration.py) |

## Graph layout

The dashboard's graph tab does not lay out the graph in the browser. The daemon
computes the geometry, stores it, and the client fetches tiles. See
[`src/task_graph/layout/`](../../../src/task_graph/layout/).

| Table | Purpose | Key columns | Invariants |
|---|---|---|---|
| `task_layouts` | One row per task per variant: its box, its position, its container path and the aggregate counts drawn on a collapsed container. | `variant`, `container_id`, `path`, `depth`, `w`/`h`, `abs_x`/`abs_y`, `agg_*` | checks: `variant IN ('all','active')`, `kind IN ('card','container','stub')` |
| `task_layout_cells` | The spatial index — which task occupies which grid cell, so a viewport maps to tasks in one query. | `cell_x`, `cell_y`, `task_id` | — |
| `project_layout_meta` | Per-project, per-variant extent and version, so a client can tell whether its tiles are stale. | `layout_version`, `extent_w`/`extent_h`, `node_count` | — |
| `layout_dirty` | The durable queue of "this project's layout needs rebuilding", written by whatever changed the graph. | `seq`, `project_id`, `task_id`, `reason` | consumed by the layout driver in the orchestrator cycle |
| `layout_jobs` | Rebuild jobs and their status, for `aq graph layout-rebuild` and the API's `jobs` endpoint. | `variant`, `kind`, `status`, `error` | — |

## Agents, profiles and sessions

A **profile** is a definition; an **agent** is a durable worker identity; a
**session** is one OS-level run of a coding-agent CLI inside a terminal. One
agent has many sessions over time, and one session may attempt several tasks.

| Table | Purpose | Key columns | Invariants |
|---|---|---|---|
| `agent_profiles` | The projection of `vault/agent-types/<id>/profile.md` into the database: harness, model, tools, MCP servers, pool bounds, lifecycle. Profiles are **global**, never project-scoped. | `harness`, `model`, `lifecycle`, `mode`, `min_active`/`max_active`/`min_per_project`, `allowed_tools`, `mcp_servers`, `default_class` | the markdown file is the source of truth; the row is rewritten from it |
| `agents` | A durable worker identity: its profile, state, current task, heartbeat and lifetime spend. | `state`, `current_task_id`, `last_heartbeat`, `harness`, `model`, `deleted_at` | `current_task_id` FK is `SET NULL` |
| `sessions` | One terminal running one coding-agent CLI: its name, lifecycle, desired state, restart ladder, work dir, instance token — and its claim. | `lifecycle`, `state`, `desired_state`, `claims`, `claim_phase`, `last_claim_epoch`, `session_key`, `restarts`, `quarantined_at`, `instance_token` | `name` is deliberately **not** unique — a relaunch reuses the name; `task_id` is nulled, not deleted, when a task is deleted |
| `task_session_attempts` | The durable per-attempt snapshot: which model, harness, provider and profile actually ran, and how the attempt ended. This is where model attribution comes from — it is never inferred from the profile. | `model`, `harness`, `provider`, `state`, `outcome`, `end_reason`, `started_at`/`ended_at` | provenance columns are **soft refs** so history outlives the session row |
| `subagent_events` | Append-only `start`/`stop` facts delivered by the harness's own subagent hooks. "How many are running" is a fold over these, not a counter. | `session_id`, `subagent_id`, `event`, `occurred_at` | `id` is a digest of `(session_id, event, subagent_id)`, so a re-delivered hook collapses onto its own row; check `event IN ('start','stop')` |
| `transcript_checkpoints` | How far the transcript watcher has read each on-disk transcript **file**. Keyed by path, not session id, so a relaunched session does not re-charge the file's whole history. | `transcript_path`, `byte_offset`, `last_entry_uuid` | keyed by path on purpose — see the module docstring |
| `agent_questions` | A question an agent asked at the end of a turn, its routing state and its answer. | `question`, `requires_human`, `state`, `answer`, `answered_by`, `delivery_token` | unique on `(session_id, instance_token, task_id, claim_epoch, turn_id)`; check on state |
| `api_session_tokens` | Scoped bearer tokens for worker sessions. Only the sha256 of the token is stored; the plaintext is returned once at mint time and never persisted. | `token_hash`, `session_id`, `task_id`, `expires_at`, `revoked_at`, `elevated` | the plaintext exists nowhere in the database |

## Workspaces

A **workspace** is a directory a task can execute in. A **workspace kind** is
its type (`project-repo`, `vault`, `readonly-dir`, or a project-defined one).
Tasks declare which kinds they need; the orchestrator acquires one of each,
all-or-nothing, in a canonical lock order.

| Table | Purpose | Key columns | Invariants |
|---|---|---|---|
| `workspaces` | One directory: its path, its kind, which task holds it and in what mode, and its worktree slot. | `workspace_path`, `kind_id`, `locked_by_task_id`, `lock_mode`, `slot_index`, `base_workspace_id` | unique on `(project_id, workspace_path)`; **partial unique** on `(base_workspace_id, slot_index)` so one slot index is used once per base checkout |
| `workspace_kinds` | The definition of a kind: writable, lockable, is it a git repo, default lock mode, auto-attach, worktree setup. Projected from `vault/[projects/<pid>/]workspace-kinds/<id>.md`. | `writable`, `lockable`, `is_git_repo`, `default_lock_mode`, `auto_attach`, `worktree_setup` | PK is `(project_id, id)` — a project override shadows the system kind |
| `task_workspace_requirements` | Which kinds a task declared, in order, with an optional alias. | `kind_id`, `position`, `alias` | ordered by `position`, which is the canonical lock order |
| `merge_slots` | One integration lease per project: who holds it and until when. | `holder_task_id`, `acquired_at`, `expires_at` | `holder_task_id IS NULL` means free; acquisition is one atomic conditional `UPDATE` |

## Messaging and human decisions

Two separate mechanisms, deliberately. **Messages** are the inbox every agent,
session and user has. **Escalations** are the durable record of a decision only
a human can make, plus the outbox that delivers it.

| Table | Purpose | Key columns | Invariants |
|---|---|---|---|
| `messages` | One inter-agent / user message: sender, recipient kind, body, delivery and read marks, thread. | `from_kind`/`from_id`, `to_kind`/`to_id`, `body`, `delivered_at`, `read_at`, `via`, `pane_open` | checks on `to_kind` (`session`/`task`/`profile`/`user`) and `from_kind`; `mark_delivered` is a compare-and-set on `delivered_at IS NULL`, which is the idempotency guard against double delivery |
| `message_discord_receipts` | The Discord message a queued message was delivered as. | `message_id`, `discord_channel_id`, `discord_message_id` | — |
| `escalations` | One incident needing a human: what happened, what was investigated, what decision is requested, and its severity and state. | `incident_key`, `summary`, `investigation`, `decision_requested`, `choices`, `severity`, `state`, `revision`, `terminal_outcome` | unique on `(project_id, incident_key)` and on `(project_id, source_kind, source_identity)`; checks bound every text length and tie `terminal_at` to a terminal state; `task_id` is a **soft ref** |
| `escalation_messages` | The immutable conversation: every inbound reply and outbound post, with the verified actor. | `direction`, `transport`, `verified_actor`, `text`, `external_message_id`, `supervisor_message_id` | unique on `(transport, external_message_id)`; there is deliberately no update method |
| `escalation_actions` | One reservation per application of verified human evidence, written *before* the action runs so an ambiguous timeout cannot replay it. | `idempotency_key`, `action_kind`, `started_revision`, `status`, `outcome` | unique on `(escalation_id, idempotency_key)`; check ties completion columns to a terminal status; no caller-authored "human" flag is stored |
| `escalation_deliveries` | The outbound outbox: one row per idempotent send, with its lease, its retry schedule and its confirmed receipt. | `dedup_key`, `kind`, `status`, `lease_owner`/`lease_expires_at`, `channel_id`/`root_message_id`/`thread_id`, `external_receipt_id` | unique `dedup_key`; checks require a receipt when `sent` and a lease exactly when `sending` |
| `digest_windows` | One evaluated installation-wide digest window — **including silent ones** — with its cursor, its lease and its receipt. | `destination`, `config_generation`, `window_start`/`window_end`, `send_status`, `suppression_reason`, `activity_cursor`, `output_hash` | unique on `(destination, config_generation, window_start, window_end)`, which is what stops a restart or a second scheduler evaluating a window twice; checks require a suppression reason when suppressed and a receipt when sent |
| `chat_analyzer_suggestions` | Suggestions derived from channel chatter, deduplicated by hash. | `suggestion_type`, `suggestion_text`, `suggestion_hash`, `status`, `suppressed_by` | — |

## Playbooks and workflows

A **playbook** is authored as markdown, compiled to an immutable artifact,
activated at a scope, and then executed as runs. The run tables are built for
one property: a resume after an ambiguous interruption must not repeat a step.

| Table | Purpose | Key columns | Invariants |
|---|---|---|---|
| `playbook_artifacts` | An immutable compiled playbook, keyed by its own sha256, with the fingerprints it was compiled against. | `artifact_sha256`, `playbook_id`, `scope`, `version`, `source_digest`, `contract_fingerprint`, `validation` | check on scope (`system`/`project`/`agent_type`/`supervisor`); content-addressed, never updated |
| `playbook_activations` | Which artifact is live at which scope, and whether it is healthy. | `scope`, `scope_identifier`, `active_artifact_sha256`, `enabled`, `health`, `reasons` | unique on `(playbook_id, scope, scope_identifier)`; check on health |
| `playbook_v2_runs` | One run: its artifact, its lifecycle, its current step and its versioned snapshot. | `lifecycle`, `mode`, `current_step_id`, `snapshot_version`, `snapshot`, `dispatch_id`, `deadline_at` | every durable advance is a compare-and-set on `snapshot_version`, so a racing resume **fails** rather than interleaving; unique on `(playbook_id, dispatch_id, rule_id)` |
| `playbook_step_receipts` | One receipt per step, tool turn, LLM call, interruption or operator decision — with tokens, cost and duration. | `receipt_kind`, `step_id`, `iteration`, `attempt`, `turn_index`, `outcome`, `idempotency_key` | unique `uq_playbook_step_receipts_boundary` on `(run_id, step_id, iteration, attempt, turn_index, receipt_kind)` — a replayed boundary is rejected *by the database*, not by in-memory state a restart would have lost |
| `playbook_waits` | A run parked on an event, a timer, a human or an agent task, with its correlation key and deadline. | `kind`, `event_type`, `correlation_key`, `match`, `deadline_at`, `state`, `claimed_event_id` | **partial unique** on `(run_id, step_id, iteration)` for active waits; checks on kind and state |
| `playbook_pending_events` | Events that arrived while an activation was unhealthy, parked with the reason and their resolution. | `reason`, `dedup_key`, `attempts`, `resolution`, `expires_at`, `protected` | **partial unique** on `(playbook_id, dedup_key)` for unresolved rows; checks on reason and resolution |
| `workflows` | The coordination view over a playbook run: stages, task ids and agent affinity. | `status`, `current_stage`, `task_ids`, `agent_affinity`, `stages` | check: `running`/`paused`/`completed`/`failed` |

## Accounting and telemetry

| Table | Purpose | Key columns | Invariants |
|---|---|---|---|
| `token_ledger` | Every charged token, per project, agent, task and model, split into input/output/cache-read/cache-write. | `tokens_used`, `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` | rows **survive task deletion** — the tokens were really spent, so dropping them would understate cost |
| `events` | The audit log: typed events with a JSON payload, scoped to project, task and agent. | `event_type`, `payload`, `timestamp` | append-only in practice; replayed by `aq event` and the event bus |
| `metrics_samples` | The fleet metrics time series — one row per `(resolution, bucket)`, with the sample body as JSON. 1s samples for an hour, 1m for 30 days, 1h for a year. | `resolution`, `bucket_ts`, `payload` | unique on `(resolution, bucket_ts)`, which is what makes the writer idempotent when a tick fires twice or a roll-up re-runs |
| `provider_usage_snapshots` | Append-only provider quota readings from two unrelated sources — a Codex transcript line and a Claude probe. | `provider`, `window`, `scope`, `used_percent`, `resets_at`, `observed_at`, `last_seen_at`, `source` | check `source IN ('transcript','probe')`; a reading identical to the newest row in its series bumps `last_seen_at` instead of inserting |

## Plugins and onboarding

| Table | Purpose | Key columns | Invariants |
|---|---|---|---|
| `plugins` | An installed plugin: where it came from, where it lives, its status, config and permissions. | `source_url`, `source_rev`, `install_path`, `status`, `permissions`, `error_message` | — |
| `plugin_data` | A plugin's own key/value storage. | `plugin_id`, `key`, `value` | — |
| `project_onboarding_requests` | The idempotency and recovery record for project creation, written *before* any filesystem or GitHub change. `created_resources` is a recovery ledger of ids and paths — never credentials or subprocess output. | `input_fingerprint`, `status`, `phase`, `created_resources`, `result`, `error` | checks: status is `pending`/`succeeded`/`failed`, and `finished_at` is set exactly when terminal. Phase and ledger may only advance while pending; `finish` is one-way |

## Integration and delivery

The largest family: 38 tables that exist to put reviewed work onto a branch
**exactly once**, survive a crash at any point, and leave auditable evidence of
what happened. The recurring devices are worth learning once:

* **Fence token** — a monotonically increasing number proving you still hold
  what you took. A write carrying a stale fence is rejected.
* **Pre-write marker** — a row written *before* an external, irreversible action
  (a push, a PR creation), so a crash mid-action is distinguishable from one
  before it.
* **Idempotency key / domain key** — a unique column making a repeated external
  operation collapse onto the row it already wrote.
* **Immutable evidence** — 29 PL/pgSQL functions and 50 triggers reject `UPDATE`
  and `DELETE` on evidence tables. Reversing one is a new revision, not an
  update.

### Batches and candidates

| Table | Purpose |
|---|---|
| `integration_batches` | One integration attempt for a project: its lifecycle, its current candidate revision, its branch, its policy and artifact snapshots. **Partial unique** on `project_id` keeps at most one active batch per project. |
| `integration_batch_members` | The sealed membership of a batch: which task, at which reviewed head and tree sha, with which review evidence. Frozen by trigger once the batch leaves `sealing`. |
| `integration_candidate_revisions` | One build attempt of a batch, with its construction base, head and CI evidence. State advances monotonically by trigger. |
| `integration_candidate_member_results` | Per member, per revision: applied, conflicted or skipped, with the generated squash sha or conflict evidence. |
| `integration_candidate_publications` | The reservation and publication of a candidate ref and its PR, keyed by an idempotency key, with a check tying PR identity to the `pr_published` state. |
| `integration_candidate_ref_mutations` | Every intended ref write — candidate, partial, repair, handoff, root main — with its expected-old sha, its nonce and its lease and branch fences. |
| `integration_candidate_resolutions` | One conflict resolution attempt by a repair delegate: the workspace, branch, fences and pushed shas, plus rejection evidence when the lineage failed validation. Rejected rows are retained as immutable audit. |

### Promotion, receipts and cleanup

| Table | Purpose |
|---|---|
| `integration_promotion_intents` | The 51-column heart of promotion: one intent to move a specific reviewed head onto a target branch, its fence, its state machine (`reserved` → `prepared` → `pushed` → `reconciled` → `committed`), and the full resolution sub-state when it conflicts. **Partial unique** on `(repository_id, target_branch)` for unresolved intents. |
| `integration_root_intent_members` | Which batch members a root promotion carries, pinned to the exact reviewed and squashed shas. Append-only by trigger. |
| `task_delivery_receipts` | Proof that one task's reviewed work reached a branch, with before/squash/after shas and its disposition (`code`, `noop`, `ineligible`, `skipped`, `failed`). Append-only; source identifiers are **soft refs** so archiving a task cannot erase the proof. |
| `integration_release_results` | The immutable record that a batch was released. |
| `integration_cleanup_items` | Everything that must be tidied after a batch — source PRs, audit PRs, remote and local refs, worktrees — each with its own claim nonce, retry schedule and irreversible-action marker. |
| `integration_outbox` | The durable event outbox for integration, deduplicated by key, with an acceptance cursor that only moves forward. |
| `integration_outbox_artifact_pins` | Pins the playbook artifacts an outbox event was produced under, so replay is interpreted the same way. |
| `integration_operation_artifact_pins` | Pins the playbook artifacts a repair operation was routed under, so a resumed operation keeps the policy it started with. |
| `development_deliveries` | Development-mode delivery: executed Git facts kept separate from task episodes. States `prepared` → `publishing` → `delivered`, or `parked`/`adopted`/`cancelled`. |

### Review, CI and repair

| Table | Purpose |
|---|---|
| `integration_review_evidence` | An approval or rejection at an exact head and tree sha, by an identified reviewer task and attempt. Append-only by trigger — the foundation everything else pins to. |
| `integration_check_evidence` | One observed CI result: producer, workflow, run, attempt, the checks themselves, and a conclusion and classification. Unique per `(producer, run, attempt, required_check_version)`. Append-only. |
| `integration_repair_operations` | One repair operation against a batch or a parent, its active stage, its state and the route it was assigned. **Partial unique** keeps one active operation per batch and per parent. |
| `integration_repair_stages` | The two ordered stages (0 and 1) of an operation: policy, writer binding, deadline, attempts and dossier. Attempt counters are monotone by trigger. |
| `integration_repair_stage_evidence` | Which check evidence a stage consumed, and what it decided. One evidence row may be counted once. |
| `integration_attestation_publications` | The reserved-then-published external check run attesting a candidate, with an execution nonce and a pre-write marker. |

### Parents, episodes and checkpoints

Hierarchical integration treats a parent task as an integration target of its
own. These tables are its control-plane identity, and they use **named
`RESTRICT` foreign keys** on purpose: this identity may not dangle.

| Table | Purpose |
|---|---|
| `integration_parent_episodes` | One collection episode for a parent task at a generation, with the pre-collection checkpoint sha. |
| `integration_child_dispositions` | Per child, what the parent decided: `noop`, `ineligible` or `skipped`. |
| `integration_parent_verifications` | One verification of a parent at an exact head and generation. Append-only. |
| `integration_parent_verification_evidence` | The check evidence a verification rests on; each evidence row is consumed once. |
| `integration_parent_operation_completions` | The completion of a parent operation, linked to the verification that closed it. |
| `integration_episode_receipt_acceptances` | Which delivery receipts an episode accepted, with the ancestry range they cover. |
| `task_integration_checkpoints` | The parent's own state: `working` → `awaiting_children` → `integration_ready` → `verifying`, with its checkpoint and verified shas. Monotone by trigger. |
| `task_branch_origins` | A task's branch reservation and materialisation on the remote, and any discard intent (`pending`/`complete`/`conflict`/`failed`). Identity and `DELETE` are frozen by trigger; only retirement and the discard columns may move. See [Data lifecycle](data-lifecycle.md#deleting-a-task-that-owns-a-branch). |

### Ownership, scheduling and rollout

| Table | Purpose |
|---|---|
| `integration_branch_owners` | Who owns a ref right now: owner, role, fence token, handoff state (`reserved`/`attached`/`handoff_pending`/`released`) and the workspace it is attached to. Fences are monotone by trigger. This is what `prepare_failed: branch not reserved` is about. |
| `project_integration_leases` | The per-project integration lease: owner, fence, heartbeat and expiry. |
| `project_integration_schedules` | When a project's integration sweep is next due, with its outstanding and catch-up request state. Sequence is monotone by trigger. |
| `integration_rollout_transitions` | Every change of a project's integration mode, with the operator, the reason and the blocker digest. Immutable. |
| `integration_history_waivers` | An operator's explicit waiver of a history blocker, with a `sha256:`-prefixed digest of exactly what was waived. Immutable. |
| `integration_history_waiver_consumptions` | Which transition consumed which waiver. One consumption per transition. |
| `integration_legacy_gate_applicability` | Whether a legacy gate still applies to a project under the current rollout, and under which waiver. |
| `integration_legacy_suppression` | The per-project projection of which legacy behaviours are suppressed. Deliberately the one reversible control in this group. |

## Related pages

* [The database](README.md) — concepts, layering and the entity diagrams
* [Query modules](queries.md) — the code that reads and writes these tables
* [Data lifecycle](data-lifecycle.md) — close, archive, delete, retention
* [Migrations](migrations.md) — how the schema changes
