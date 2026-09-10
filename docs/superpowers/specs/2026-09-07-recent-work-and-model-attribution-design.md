# Recent work and model attribution in the Tasks tab

<!-- aq:historical -->
> **Historical design record.** This spec describes one feature as it was
> designed, not as the code stands today. Start at [the documentation
> home](../../README.md) for current behaviour; see [historical
> material](../../history/README.md).

Status: implemented (2026-09-07) · Task: `nimble-current`

## Problem

"What did we get done yesterday, and which models did it?" had no answer
short of reading the graph tab task by task. The Tasks tab lists the whole
backlog with no notion of time, and the model an agent actually ran on is not
on the task row at all — it lives one level down, on the session attempt.

## What the data already knows

- `tasks` / `archived_tasks` — status and `updated_at`.
- `task_completion_records` — the outcome and `completed_at` of each close.
- `task_session_attempts` — the durable per-attempt snapshot written at launch
  (`model`, `intelligence_class`, `llm_provider`, `harness`, `agent_name`,
  `started_at` / `ended_at`, `outcome`). This is the only place that records
  what actually ran, and it is what makes retries legible: one row per attempt,
  so a task retried on a second model has both.

Nothing new is persisted. There is no migration.

## The read

`ActivityQueryMixin.list_recent_task_activity(since, until, project_id, limit)`
(`src/database/queries/activity_queries.py`) returns `(items, total)`.

A task is *touched* by the window when any of:

1. a session attempt **overlaps** it — `started_at <= until` and
   (`ended_at is NULL` or `ended_at >= since`). The still-running case is why
   the predicate is an overlap and not `started_at >= since`: an attempt that
   began three days ago and has not exited is exactly the in-progress work the
   view must show.
2. a completion record landed inside it, or
3. the task row's own `updated_at` falls inside it (a status change with no
   session at all — a pause, a manual edit, a playbook transition).

Items carry the task's identity and status, the completion outcome, and only
the attempts that overlap the window — an older attempt on a task that is
active today is *not* reported, because the window bounds the work, not the
task. `last_activity_at` is the newest of those signals and orders the list.

Archived tasks are read from `archived_tasks` and flagged `archived: true`;
attempts outlive archival, so their history survives it.

### Attribution

`models` is the distinct set of `attempts[].model`, newest attempt first.
`unattributed_attempts` counts the attempts that recorded no model.

Attribution is **reported, never inferred**. A NULL `model` — a legacy row, or
a harness that never told us — is shown as unattributed rather than filled in
from the profile's configured model, which would claim a fact the system does
not have. A task with no attempts at all reads "no agent session", which is a
different statement from "unattributed".

## Surfaces

- **Command** `task_recent_activity` (`src/commands/task_commands.py`):
  `hours` (default 24, capped at 31 days), `project_id` (defaults to the
  caller's active project), `limit` (default 200, max 1000). Adds `by_model`
  totals — tasks and attempts per model across the window, with the
  unattributed bucket as `model: null`. Reaches CLI (`aq task recent-activity`,
  with a Rich table), MCP, and `POST /api/task/recent-activity` through the
  usual codegen path.
- **Tasks tab**: a *Time range* select (Any time / Last 24 hours / Last 7 days)
  in `TaskToolbar`. A range is a `window=` URL parameter, so the view is
  linkable. While one is set the table's row source is the activity read
  rather than the graph snapshot, a labelled banner states the exact interval,
  and two columns appear: **Models** (one chip per model, plus an
  unattributed marker and an attempt count when a task was retried) and
  **Last activity** (relative age, outcome, archived marker). Rows still open
  the task-detail pane, so results stay one click away.

A time range implies "show completed": the range is a claim about work done in
it, so hiding the finished half would make it a lie. The implication lives in
`matchesTask` (and is mirrored by the now-disabled checkbox) so it holds
wherever the filters are applied.

## Tests

- `tests/test_task_activity.py` — window edges (`since` and `until` inclusive,
  work that ended before the window excluded), the still-running attempt,
  status-change-only activity, per-window attempt scoping, multi-attempt and
  multi-model tasks, missing attribution, archived tasks, project scoping,
  ordering, `limit`/`total`/`truncated`, command argument validation, and the
  typed API response shape.
- `tests/test_cli_formatters.py` — the Rich table on a sparse payload.
- `dashboard/src/pages/command-center/__tests__/` — `taskFilters` (window
  parsing, unknown values rejected, completed implied), `activityFormat`
  (relative-age boundaries), `Tasks` (window mode renders models, retries and
  unattributed attempts; no fetch without a range), `TaskToolbar` (the range
  reaches the URL and clears with the other filters).
