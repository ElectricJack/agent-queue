---
id: stuck-task-watch
name: Stuck-task watch
version: 1
scope: system
enabled: true
triggers:
  - timer.15m
state_schema_version: 1
required_capabilities:
  - list_projects
  - list_tasks
  - get_stuck_tasks
  - doctor_run
  - playbook_state_list
  - playbook_state_notify
  - message_send
---

# Stuck-task watch

This deliberately imported policy observes every fifteen minutes. It is never a
first-install default or a readiness requirement. Installing or activating it is
an operator action after reviewing this source and its pinned artifact. Historical
acceptance-policy lineage: `stark-vault-94`; implementation: `bright-grove-21.15`.

Notify `session:supervisor-global` through `playbook_state_notify`, which also
requires `message_send`. Choose `to_kind` `session` and `to_id` `supervisor-global`.
The `repeat_after_seconds` policy is `null`. Namespace version is 1; ordinary
recompile, restart and disable/re-enable retain episodes and suppression. The
reminder recording has its own playbook identity and therefore its own namespace.
No script, model, repair, task creation, restart or provider mutation is allowed.

## Rule: watch-stalled-work

Run on `timer.15m`. Every scan uses the run's frozen `context.observed_at` and
`context.observation_id`; snapshot continuations reuse the original request and
immutable tokens. All loops use `max_iterations` `10000`, halt on body failure,
and never aggregate full page payloads. All snapshot reads use `pagination`
`snapshot`, with `page_size` `500`. Source-local manifests remain bounded.

## Inventory and pre-scan state

Call `list_projects`, bind `inventory`, require `complete` `eq` `true`, traverse
all `page_tokens`, and require every project page complete. Select project
manifest rows with `status` `eq` `ACTIVE`. Incomplete inventory fails before any
notification or clearing; absence from a failed inventory never means inactive.

Call `playbook_state_list` with `record_space` `notification` before diagnostic
scans, bind `previous`, require completeness, then traverse every saved state
page. Keep `previous.identity_manifest` unchanged all tick. Each prior row carries
`key`, `revision`, `active`, and `source_partition.project_id` and
`source_partition.source`. Only rows with `active` `eq` `true` in the current
active project and source partition can be cleared. Inactive state is untouched.

## Task partitions

For each active project, snapshot `get_stuck_tasks` into `stuck_scan` with
`assigned_threshold_seconds` `1800`, `in_progress_threshold_seconds` `7200`, and
`now` bound to `context.observed_at`. Require complete, read every page's `stuck`
array, and select exactly: (`status` `eq` `ASSIGNED` and `seconds_in_state` `gt`
`1800`) or (`status` `eq` `IN_PROGRESS` and `seconds_in_state` `gt` `7200`).
An aged ORPHANED entry qualifies. STUCK READY and younger ORPHANED entries do not.
Keep the producer's hold/container and scheduling-paused exclusions.

Then independently snapshot `list_tasks` into `blocked_scan` for that project's
`status` `BLOCKED`, `display_mode` `flat`; traverse every `tasks` page. Every
BLOCKED task qualifies regardless of age. Do not substitute list_active_tasks.

For each selected row call `playbook_state_notify` action `present`, using the
canonical string `task:<project_id>:stuck:<id>` or
`task:<project_id>:blocked:<id>` as key. Project/task identifiers are canonical
AQ identifiers. Save source partition project_id and source (`stuck` or `blocked`),
the frozen observation identity/time, and the evidence as `last_value`. Messages
name the project, task and source and carry the full readable finding.

Only after every page and present notification succeeds, map the SAME predicate
and key construction over the corresponding compact `identity_manifest`. Compute
`diff` of prior active state rows and mapped current keys with `key_path` `key`.
For each absent key, notify action `cleared` with the original pre-scan `revision`
as `expected_revision`. A `conflict` or `state.stale_observation` defers to the
next tick; never reread state or retry the old scan with a fresh revision.

A failed or incomplete initial task scan skips only that source partition. A
page or present-notification failure halts the run before its clear continuation;
earlier completed independent partitions remain committed.

## Doctor partitions

Before the task partitions, snapshot `doctor_run` with `group`
`task_observations_v1`, `now` bound to `context.observed_at`, and bind `doctor`.
A failure result may still describe complete partitions. Require an existing
snapshot_id and no oversize artifacts before inspecting coverage. An unavailable
scan skips doctor processing and cannot clear any doctor record.

For each active project, independently gate these five partitions:
`claims.holder_consistency` / `holder_inconsistency`, and `stall.sweep` /
`unclaimed_work`, `undelivered_blocker`, `restore_branch`, `worker_relaunch_loop`.
A partition must have applicable project (or global) coverage, and no applicable
coverage row with `complete` `eq` `false`. Match both `check_id` and `kind`.
No other diagnostic kind is an episode. Never infer absence from another probe.

Traverse every token with `kind` `eq` `findings` in nested foreach loops; require
that each read returns `page_kind` `eq` `findings`. Filter payloads and manifest
identically by project_id, check_id and kind. Notify present using the producer's
`key`, evidence and source partition `doctor:<check_id>:<kind>`. Then diff against
the saved prior active keys for that partition and clear absent keys with their
pre-scan revisions. A missing/expired/failed page halts before clearing. An
incomplete partition is skipped independently of complete ones. No flattened
fleet array, accumulator, helper command or watch-specific Python is involved.

## Failure handling, uniformly

There are no retries within a tick. A malformed result, input/budget failure,
unauthorized command or recipient, notification rollback or unexpected failure
ends visibly in `failed`. Clear CAS conflicts alone defer the key to the next
tick. Complete runs end in `completed`; snapshot and state limits never silently
truncate successful observations. Persisted state is the only cross-tick memory.

## Exact result paths and policy clauses

Paths: `active`, `artifacts`, `check_id`, `complete`, `coverage`, `error_code`, `findings`, `id`, `identity_manifest`, `key`, `kind`, `page_kind`, `page_token`, `page_tokens`, `project_id`, `revision`, `seconds_in_state`, `snapshot_id`, `source_partition.project_id`, `source_partition.source`, `status`, `stuck`, `tasks`.

- `active` `eq` `true`.
- `complete` `eq` `false`.
- `complete` `eq` `true`.
- `error_code` `in` `["conflict", "state.stale_observation"]`.
- `kind` `eq` `findings`.
- `page_kind` `eq` `findings`.
- `project_id` `in` `[null]`.
- `seconds_in_state` `gt` `1800`.
- `seconds_in_state` `gt` `7200`.
- `source_partition.source` `eq` `blocked`.
- `source_partition.source` `eq` `stuck`.
- `status` `eq` `ACTIVE`.
- `status` `eq` `ASSIGNED`.
- `status` `eq` `IN_PROGRESS`.

Literal labels: ` in project `, `:`, `; evidence: `, `; source `, `ACTIVE`, `ASSIGNED`, `BLOCKED`, `Cleared `, `IN_PROGRESS`, `Observed `, `Stuck-task observation`, `Stuck-task observation cleared`, `action`, `assigned_threshold_seconds`, `blocked`, `blocked_clear_item`, `blocked_clear_result`, `blocked_item`, `blocked_payload`, `blocked_scan`, `blocked_token`, `body`, `candidate`, `check_id`, `claims.holder_consistency`, `cleared`, `coverage`, `display_mode`, `doctor`, `doctor:`, `doctor_clear_item`, `doctor_clear_result`, `doctor_item`, `doctor_payload`, `doctor_run`, `doctor_token`, `expected_revision`, `finding`, `findings`, `flat`, `get_stuck_tasks`, `group`, `holder_inconsistency`, `in_progress_threshold_seconds`, `inventory`, `inventory_payload`, `inventory_token`, `key`, `key_item`, `kind`, `last_value`, `list_projects`, `list_tasks`, `message`, `message_send`, `notification`, `now`, `observation_id`, `observed_at`, `page_size`, `page_token`, `pagination`, `part`, `partition`, `playbook_state_list`, `playbook_state_notify`, `present`, `previous`, `prior`, `project`, `project_id`, `record_space`, `restore_branch`, `session`, `snapshot`, `snapshot_id`, `source`, `source_partition`, `stall.sweep`, `state_payload`, `state_token`, `status`, `stuck`, `stuck_clear_item`, `stuck_clear_result`, `stuck_item`, `stuck_payload`, `stuck_scan`, `stuck_token`, `subject`, `supervisor-global`, `task:`, `task_observations_v1`, `to_id`, `to_kind`, `token`, `unclaimed_work`, `undelivered_blocker`, `worker_relaunch_loop`.
