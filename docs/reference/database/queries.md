# Query modules

Every SQL statement AQ issues lives in one of the 49 query modules under
[`src/database/queries/`](../../../src/database/queries/). Nothing above them
builds SQL, and nothing below them makes a decision. This page says what each
one owns, so you can go from "where does this row come from?" to a file.

If you want the tables instead, see [Table families](tables.md). For the
layering these files sit in, see [the database overview](README.md#how-code-reaches-the-database).

## How a query module works

A query module is a **mixin class**: a plain class with async methods that reach
the database through `self._engine`. The adapter
([`adapters/postgresql.py`](../../../src/database/adapters/postgresql.py))
inherits from 47 of them, so every method ends up on one object:

```python
class PostgreSQLDatabaseAdapter(
    IntegrationControlQueriesMixin,
    HierarchyQueryMixin,
    TaskQueryMixin,
    ...,            # 47 in total
    TransactionQueryMixin,
):
```

Two modules break the pattern deliberately:
[`proposal_queries.py`](../../../src/database/queries/proposal_queries.py) and
[`triage_queries.py`](../../../src/database/queries/triage_queries.py) are
module-level functions taking the adapter as an argument, so callers and tests
can use them without building an adapter subclass.

### The two transaction shapes

This is the single most useful thing to know when reading these files.

**Self-contained.** The method opens `engine.begin()`, does its work, commits.
Most reads and simple writes look like this. Safe to call from anywhere.

```python
async def get_task(self, task_id: str) -> Task | None:
    async with self._engine.begin() as conn:
        ...
```

**Caller-transaction-owned.** The method takes `conn` and never commits. It
exists because a guard condition must be read and acted on atomically. The
caller opens the boundary with `db.immediate()`, and is then responsible for the
post-commit side effects (blocked-state event logging, container settlement
notifications, ready-frontier notifications).

```python
async def set_parent(self, task_id, parent_id, *, conn): ...
```

The table below shows how many public methods of each module take `conn` — a
high number means the module is a *building block* other code composes, not a
standalone API. `hierarchy_queries` (15 of 26) and `claim_queries` (12 of 16)
are the extremes, and for the same reason: both write several tables that must
agree.

### The house rules every module follows

* **Timestamps are Python floats.** No database `now()` default anywhere.
* **A conditional `UPDATE` is how a race is decided.** `rowcount == 1` means
  "I won"; zero means "someone else did", and the caller says so rather than
  retrying blindly.
* **A projection is recomputed in the same transaction that could invalidate
  it.** That is why so many mutating methods end in a `blocked_state` call.
* **JSON is for frozen evidence.** If a value is filtered or ordered on, it is a
  column.

## Core: tasks and the work graph

| Module | Owns | Shape |
|---|---|---|
| [`task_queries.py`](../../../src/database/queries/task_queries.py) | The `tasks` table and its satellites — create (with the routing gate in the same transaction), list and filter, update, pause/resume with a hold snapshot, the state-machine-checked `transition_task`, delete, metadata, labels, context, dedup-key lookup. 41 public methods; the largest module in the layer. | mixed (5 take `conn`) |
| [`hierarchy_queries.py`](../../../src/database/queries/hierarchy_queries.py) | Parent/child membership — the **single writer** of `tasks.parent_task_id`, always in the same transaction as the `parent-child` edge, the blocked recompute and container settlement. Also subtree reads, depth and height, container settlement, `abandon_subtree`, and the hierarchy guards that fence legacy bulk writers. | caller-owned (15 of 26) |
| [`dependency_queries.py`](../../../src/database/queries/dependency_queries.py) | Typed edges on `task_dependencies`. Every mutation recomputes `is_blocked` so the projection can never lag the graph. | mostly self-contained |
| [`blocked_state.py`](../../../src/database/queries/blocked_state.py) | The `tasks.is_blocked` projection itself: blocked ⇔ some blocking edge is unsatisfied or some attached gate is unresolved. Graph blockedness only — capacity, budget and cooldown are `aq task explain`'s business, not a column's. | caller-owned |
| [`gate_queries.py`](../../../src/database/queries/gate_queries.py) | `gates` and `task_gates`. Every mutation recomputes the projection for the gate's waiters in the same transaction. | mixed |
| [`task_comment_queries.py`](../../../src/database/queries/task_comment_queries.py) | Agent-append-only comments, operator edits, and fenced description updates. | self-contained |
| [`result_queries.py`](../../../src/database/queries/result_queries.py) | `task_results` (per-attempt output) and `task_completion_records` (`save_task_completion` — append only, never updated). | self-contained |
| [`archive_queries.py`](../../../src/database/queries/archive_queries.py) | Moving a terminal subtree from `tasks` into `archived_tasks`, the retention sweep, and reading or permanently deleting an archived task. | self-contained |
| [`activity_queries.py`](../../../src/database/queries/activity_queries.py) | One read: which tasks moved in a time window and which model did it, spanning `tasks`, `archived_tasks`, `task_completion_records` and `task_session_attempts`. An attempt with no recorded model is counted **unattributed**, never filled in from the profile. | self-contained |
| [`proposal_queries.py`](../../../src/database/queries/proposal_queries.py) | Staged task/edge proposals and cross-graph cycle detection before they are committed. Module-level functions, not a mixin. | self-contained |
| [`triage_queries.py`](../../../src/database/queries/triage_queries.py) | The atomic, history-preserving lifecycle of a project's routing task. Open routing gates are the durable inbox; a saved gate-id set stops unchanged work restarting a completed task every tick. Module-level functions, not a mixin. | self-contained |
| [`task_recovery_queries.py`](../../../src/database/queries/task_recovery_queries.py) | Durable supervisor incidents and bounded recovery. No terminal I/O here — this module only records. | self-contained |
| [`assignment_route_queries.py`](../../../src/database/queries/assignment_route_queries.py) | The current successful assignment-playbook decision for a task. | caller-owned |
| [`layout_queries.py`](../../../src/database/queries/layout_queries.py) | The graph layout tables: publish a whole layout atomically, read tiles and extents, consume dirty marks, track rebuild jobs. 33 public methods. | mixed |

## Execution: claims, agents, sessions

| Module | Owns | Shape |
|---|---|---|
| [`claim_queries.py`](../../../src/database/queries/claim_queries.py) | The claim transaction and the epoch fence. On a caller-owned connection: take the session slot with a conditional `UPDATE` first, then the task, then agent, workspace and metadata. `activate_claim` and `release_claim` race by design — the conditional `UPDATE`s make exactly one win. | caller-owned (12 of 16) |
| [`session_queries.py`](../../../src/database/queries/session_queries.py) | The `sessions` row: create, update, list by pool, quarantine. Restart counters live on the row, not in memory, so the stall/crash ladder survives a daemon restart — which is exactly when a crash-looping session is most likely running. `sessions.name` is deliberately non-unique. | mixed |
| [`task_session_queries.py`](../../../src/database/queries/task_session_queries.py) | `task_session_attempts` — the durable task/session association, written inside lifecycle transactions. | caller-owned (2 of 7) |
| [`agent_queries.py`](../../../src/database/queries/agent_queries.py) | Agent identity, state and heartbeat. | self-contained |
| [`profile_queries.py`](../../../src/database/queries/profile_queries.py) | The `agent_profiles` projection of the vault's markdown profiles. | self-contained |
| [`subagent_queries.py`](../../../src/database/queries/subagent_queries.py) | Append-only subagent `start`/`stop` events, and the folds ("how many are running") the flock reads. A mutable counter would be permanently wrong after one re-delivered hook. | self-contained |
| [`transcript_queries.py`](../../../src/database/queries/transcript_queries.py) | The transcript watcher's read position, keyed by **file path** rather than session id — three successive supervisor incarnations each re-charged the same 133 ledger rows before this existed. | self-contained |
| [`api_session_token_queries.py`](../../../src/database/queries/api_session_token_queries.py) | Minting, looking up and revoking scoped session tokens. Only the sha256 is stored; `mint` returns the plaintext once. | self-contained |
| [`agent_question_queries.py`](../../../src/database/queries/agent_question_queries.py) | Durable question identity, the answer compare-and-set, and transport receipts. | mostly self-contained |
| [`token_queries.py`](../../../src/database/queries/token_queries.py) | The token ledger and its roll-ups. | self-contained |

## Workspaces

| Module | Owns | Shape |
|---|---|---|
| [`workspace_queries.py`](../../../src/database/queries/workspace_queries.py) | Workspace CRUD and locking — acquire, release, reap, slot affinity. 19 public methods. | mixed |
| [`workspace_kinds_queries.py`](../../../src/database/queries/workspace_kinds_queries.py) | Workspace-kind CRUD and resolution, including project override shadowing a system kind. | self-contained |
| [`task_requirements_queries.py`](../../../src/database/queries/task_requirements_queries.py) | A task's declared `requires_kinds`, in canonical lock order. | mixed |
| [`merge_slot_queries.py`](../../../src/database/queries/merge_slot_queries.py) | The per-project integration lease: seed the row, then one atomic conditional `UPDATE` that succeeds iff the slot is free, already yours, or expired. `rowcount == 1` means acquired. | self-contained |

## Projects and installation

| Module | Owns | Shape |
|---|---|---|
| [`project_queries.py`](../../../src/database/queries/project_queries.py) | Project CRUD, constraints and the cascading project delete. | self-contained |
| [`repo_queries.py`](../../../src/database/queries/repo_queries.py) | The legacy `repos` table's CRUD, still read by a few path-resolution fallbacks. | self-contained |
| [`onboarding_queries.py`](../../../src/database/queries/onboarding_queries.py) | The project-onboarding saga's idempotency record. Deliberately strict: a request starts `pending`, its phase and owned-resource ledger may only advance while pending, and `finish` is one-way. A retried request inspects the first record instead of starting a second saga. | self-contained |
| [`plugin_queries.py`](../../../src/database/queries/plugin_queries.py) | Installed plugins and their key/value data. | self-contained |
| [`chat_queries.py`](../../../src/database/queries/chat_queries.py) | Chat-analyzer suggestions and their suppression. | self-contained |

## Messaging, escalations and the digest

| Module | Owns | Shape |
|---|---|---|
| [`message_queries.py`](../../../src/database/queries/message_queries.py) | The `messages` table. `mark_delivered` is a compare-and-set on `delivered_at IS NULL` — that one predicate is the whole guard against a nudge and a prompt both delivering the same message. | self-contained |
| [`escalation_queries.py`](../../../src/database/queries/escalation_queries.py) | Transport-neutral escalations, delivery leases and digest windows, 27 methods. Accepting a verified reply appends the immutable conversation fact, advances the escalation revision **and** queues the supervisor message in one transaction. | self-contained |
| [`digest_queries.py`](../../../src/database/queries/digest_queries.py) | The only part of the digest that touches rows. Two questions: what *happened* in the window (completions, attempt starts, `kind='progress'` comments, PR milestones — each carrying the identity of its source row so a replay cannot double-count), and what is *executing now* (a live, non-stale attempt on a running session, never `updated_at` churn, a heartbeat, a stalled session or a worker parked on a human answer). | self-contained |

## Playbooks and workflows

| Module | Owns | Shape |
|---|---|---|
| [`playbook_run_queries.py`](../../../src/database/queries/playbook_run_queries.py) | Durable V2 run state, 36 methods. Every durable advance is **one** `immediate()` block containing a compare-and-set on `snapshot_version` plus exactly one receipt insert. A resume that raced another writer loses the CAS and raises; a replayed boundary is rejected by the unique index, by the database rather than by memory a restart would have lost. | mixed |
| [`playbook_artifact_queries.py`](../../../src/database/queries/playbook_artifact_queries.py) | Immutable compiled artifacts and explicit activation records. | mixed |
| [`workflow_queries.py`](../../../src/database/queries/workflow_queries.py) | The coordination-workflow view over a run: create, fetch, transition status, append a task. | self-contained |
| [`event_queries.py`](../../../src/database/queries/event_queries.py) | The audit log. | mostly self-contained |

## Telemetry

| Module | Owns | Shape |
|---|---|---|
| [`metrics_queries.py`](../../../src/database/queries/metrics_queries.py) | Two things that must not be confused: the per-second **aggregates** the sampler folds into one sample (each a single grouped statement over an indexed column, because they run once a second next to twenty live agents), and the idempotent **store** for `metrics_samples` plus its range read and retention sweep. Nothing here interprets a sample. | self-contained |
| [`provider_usage_queries.py`](../../../src/database/queries/provider_usage_queries.py) | Append-only provider quota readings. A reading identical to the newest row in its series bumps `last_seen_at` instead of inserting — otherwise an idle fleet would write a row a second carrying no new information — but the duplicate is still recorded as evidence the probe ran. | self-contained |

## Integration and delivery

Seven modules, split by *who owns the transaction* rather than by table, because
the integration services compose several of them inside one boundary.

| Module | Owns | Shape |
|---|---|---|
| [`integration_state_queries.py`](../../../src/database/queries/integration_state_queries.py) | Read projections over durable hierarchical-integration state. | reads |
| [`integration_delivery_queries.py`](../../../src/database/queries/integration_delivery_queries.py) | Review evidence, promotion intents and delivery receipts — the evidence spine. 18 methods. | mixed |
| [`integration_control_queries.py`](../../../src/database/queries/integration_control_queries.py) | Rollout controls: waivers, transitions, consumptions, legacy suppression. | caller-owned (6 of 9) |
| [`integration_train_queries.py`](../../../src/database/queries/integration_train_queries.py) | The projections used to seal an integration train atomically. | caller-owned |
| [`integration_schedule_queries.py`](../../../src/database/queries/integration_schedule_queries.py) | Per-project sweep schedules and their outstanding/catch-up requests. | caller-owned |
| [`integration_reconciliation_queries.py`](../../../src/database/queries/integration_reconciliation_queries.py) | Bounded, read-only selectors for one reconciliation tick. Bounded on purpose: a tick must not scan the whole control plane. | reads |
| [`hierarchy_queries.py`](../../../src/database/queries/hierarchy_queries.py) (again) | The hierarchy-side guards integration relies on: `guard_integration_mutation`, `materialized_origin_when_hierarchical`, `hierarchy_runnable_task_ids`. | caller-owned |

## Infrastructure

| Module | Owns |
|---|---|
| [`transaction_queries.py`](../../../src/database/queries/transaction_queries.py) | `immediate()` — the write transaction every caller-owned method is composed inside. On PostgreSQL it is `engine.begin()`. |
| [`__init__.py`](../../../src/database/queries/__init__.py) | Package marker for the mixins. Its docstring states the contract they share: backend-neutral SQLAlchemy Core over `self._engine`, or an `AsyncConnection` for the caller-owned shape. |

## Adding a query

1. Find the module that owns the table. If the subject is genuinely new, add a
   file and a mixin class, and register it in the adapter's base-class list.
2. Decide the transaction shape. If your method reads a condition and then acts
   on it, take `conn` and let the caller own the boundary.
3. If the write can change whether a task is blocked, recompute the projection
   in the same transaction.
4. Return domain dataclasses from [`src/models.py`](../../../src/models.py), not
   raw rows, for anything a command will hand back to a caller.
5. Add the method to `DatabaseBackend` in
   [`base.py`](../../../src/database/base.py) so type checkers see it.
6. Test the module's own file:

```bash
aq test tests/test_database.py tests/test_hierarchy_queries.py
aq test tests/test_claim_commands.py tests/test_gate_queries.py
aq test tests/test_blocked_state.py tests/test_dependency_queries.py
```

## Related pages

* [The database](README.md) · [Table families](tables.md) ·
  [Data lifecycle](data-lifecycle.md) · [Migrations](migrations.md)
* [Module catalog — database](../modules/database.md) — one row per file
