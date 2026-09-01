# Playbook Intelligence Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every otherwise assignable task through an LLM playbook that selects `intelligence_class` and optional provider, then leave profile, agent, capacity, workspace, and fairness decisions to the existing orchestrator.

**Architecture:** Add a persisted derived route beside each task, with one pure resolver that gives explicit task classes precedence and rejects stale playbook decisions. A lightweight coordinator invokes a deterministic assignment-playbook graph in bounded project batches before scheduling; push scheduling, pool claims, and pre-launch checks consume the same effective route while retaining their existing selection algorithms.

**Tech Stack:** Python 3.12, asyncio, SQLAlchemy Core, Alembic, SQLite/PostgreSQL, Pydantic playbook models, pytest/pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-31-playbook-intelligence-routing-design.md`

## Global Constraints

- `tasks.intelligence_class` remains the explicit user requirement and always wins without an LLM call.
- The assignment playbook may return only `intelligence_class`, optional provider, echoed integrity fields, and a short reason; it never selects `profile_id`, workspace, lifecycle, pool, agent, or session.
- Missing, failed, invalid, or stale derived routes never fall back to profile or agent defaults for assignment eligibility.
- Batch at most 25 tasks from one project in one direct PlaybookRunner call, with no command tools and no projected playbook-run task.
- Provider usage comes only from AQ's existing observations; this feature makes no external quota or billing calls.
- Keep pool claims and push scheduling, and apply identical effective-route semantics to both.
- Remove only the default pipeline's `task-created-routing` and `worker-filed-triage` rules; preserve unrelated rules and historical triage data.
- Schema changes are additive and supported by SQLite, PostgreSQL, and SQLite-to-PostgreSQL copying.
- Baseline note: before implementation, `tests/perf/test_claim_statements.py::TestClaimStatementBudgets::test_claim_happy_path_statement_budget[sqlite]` reports 24 statements against an existing budget of 19.

---

### Task 1: Persist and resolve effective assignment routes

**Files:**
- Create: `src/assignment_routing.py`
- Create: `src/database/queries/assignment_route_queries.py`
- Create: `migrations/versions/<revision>_add_assignment_routes.py`
- Create: `tests/test_assignment_routing.py`
- Create: `tests/test_assignment_route_queries.py`
- Modify: `src/models.py`
- Modify: `src/database/tables.py`
- Modify: `src/database/base.py`
- Modify: `src/database/queries/__init__.py`
- Modify: the concrete SQLAlchemy database adapter mixin lists under `src/database/`
- Modify: `scripts/migrate_sqlite_to_pg.py`
- Modify: migration-schema tests that assert the current head

**Interfaces:**
- Produces: `TaskAssignmentRoute`, `AssignmentOption`, and `EffectiveAssignmentRoute` immutable dataclasses.
- Produces: `assignment_input(task: Task) -> dict[str, object]`, `assignment_input_hash(task: Task) -> str`, `options_hash(options: Sequence[AssignmentOption]) -> str`, and `resolve_effective_route(task: Task, saved: TaskAssignmentRoute | None, current_options_hash: str) -> EffectiveAssignmentRoute | None`.
- Produces database methods `get_task_assignment_route(task_id: str, *, conn=None)`, `list_task_assignment_routes(task_ids: Sequence[str], *, conn=None)`, and `upsert_task_assignment_routes(routes: Sequence[TaskAssignmentRoute], *, conn)`.
- Consumes only canonical task fields, so scheduler and claim code can reuse it without the coordinator.

- [ ] **Step 1: Write failing pure-resolution and query tests**

```python
def test_explicit_class_wins_over_saved_route(task, saved_route):
    task.intelligence_class = "deep-high"
    route = resolve_effective_route(task, saved_route, "catalog-v2")
    assert route == EffectiveAssignmentRoute(
        task_id=task.id,
        intelligence_class="deep-high",
        provider=None,
        source="explicit",
    )

def test_saved_route_requires_matching_task_revision_and_options_hash(task, saved_route):
    assert resolve_effective_route(task, saved_route, saved_route.options_hash) is not None
    saved_route = replace(saved_route, task_updated_at=task.updated_at - 1)
    assert resolve_effective_route(task, saved_route, saved_route.options_hash) is None

@pytest.mark.asyncio
async def test_upsert_replaces_current_route_and_task_delete_cascades(db, task, route):
    async with db.immediate() as conn:
        await db.upsert_task_assignment_routes([route], conn=conn)
    assert (await db.get_task_assignment_route(task.id)).playbook_run_id == route.playbook_run_id
    await db.delete_task(task.id, cascade=True)
    assert await db.get_task_assignment_route(task.id) is None
```

- [ ] **Step 2: Run the new tests and verify missing symbols/schema fail**

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/test_assignment_routing.py tests/test_assignment_route_queries.py -q`

Expected: collection or assertion failure because the route model, table, and resolver do not exist.

- [ ] **Step 3: Add the route model, table, migration, and query mixin**

```python
@dataclass(frozen=True)
class TaskAssignmentRoute:
    task_id: str
    project_id: str
    input_hash: str
    task_updated_at: float
    options_hash: str
    intelligence_class: str
    provider: str | None
    playbook_id: str
    playbook_version: int
    playbook_run_id: str
    reason: str
    decided_at: float

def resolve_effective_route(task, saved, current_options_hash):
    explicit = (task.intelligence_class or "").strip()
    if explicit:
        return EffectiveAssignmentRoute(task.id, explicit, None, "explicit")
    if saved is None or saved.task_updated_at != task.updated_at:
        return None
    if saved.input_hash != assignment_input_hash(task):
        return None
    if saved.options_hash != current_options_hash:
        return None
    return EffectiveAssignmentRoute(
        task.id, saved.intelligence_class, saved.provider, "playbook",
        saved.input_hash, saved.playbook_run_id,
    )
```

Create `task_assignment_routes` with `task_id` as its primary key, cascading foreign keys to `tasks`, `projects`, and `playbook_runs`, and an index on `project_id`. Register the mixin in every SQLAlchemy adapter and add the table to `scripts/migrate_sqlite_to_pg.py` after its dependencies.

- [ ] **Step 4: Run route, migration, and database contract tests**

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/test_assignment_routing.py tests/test_assignment_route_queries.py tests/test_migration_work_graph.py tests/test_database_protocol.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the persistence slice**

```bash
git add src/assignment_routing.py src/models.py src/database migrations scripts/migrate_sqlite_to_pg.py tests
git commit -m "feat: persist effective assignment routes"
```

### Task 2: Compile and select assignment playbooks

**Files:**
- Create: `src/playbooks/assignment_compiler.py`
- Create: `src/prompts/default_playbooks/default-assignment-routing.md`
- Create: `tests/test_assignment_playbook_compiler.py`
- Modify: `src/playbooks/compiler.py`
- Modify: `src/playbooks/manager.py`
- Modify: `src/playbooks/models.py`
- Modify: `src/vault.py`
- Modify: `src/models.py`
- Modify: `src/database/tables.py`
- Modify: `src/database/queries/project_queries.py`
- Modify: `src/api/models/project.py`
- Modify: `src/commands/project_commands.py`
- Modify: the Task 1 Alembic migration
- Modify: `tests/test_playbook_compiler_scope.py`
- Modify: project command/API tests

**Interfaces:**
- Produces: `compile_assignment_playbook(markdown: str, *, existing_version: int = 0) -> CompilationResult`.
- Produces: `select_assignment_playbook(manager: PlaybookManager, project: Project) -> CompiledPlaybook`, raising `AssignmentPlaybookError` for an invalid explicit override.
- Produces: `Project.assignment_playbook_id: str | None`; null resolves to `default-assignment-routing`.
- Consumes: existing `CompiledPlaybook`, `PlaybookManager.get_playbook()`, and `PlaybookManager.get_scope_identifier()`.

- [ ] **Step 1: Write failing compiler, default-install, and project-selection tests**

```python
def test_assignment_markdown_compiles_to_one_llm_node():
    result = compile_playbook(ASSIGNMENT_MARKDOWN)
    assert result.success
    assert result.playbook.kind == "assignment-routing"
    assert result.playbook.role == "assignment-routing"
    assert list(result.playbook.nodes) == ["choose", "done"]
    assert result.playbook.nodes["choose"].tools == []

def test_project_override_must_be_enabled_assignment_playbook(manager, project):
    project.assignment_playbook_id = "review-code"
    with pytest.raises(AssignmentPlaybookError):
        select_assignment_playbook(manager, project)

def test_default_playbook_is_seeded_write_if_absent(tmp_path):
    result = ensure_default_playbooks(str(tmp_path))
    assert "default-assignment-routing.md" in result["created"]
```

- [ ] **Step 2: Run focused tests and verify they fail**

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/test_assignment_playbook_compiler.py tests/test_playbook_compiler_scope.py tests/test_vault.py -q`

Expected: FAIL because `kind: assignment-routing` has no deterministic compiler and the project has no override field.

- [ ] **Step 3: Implement the fixed compiler and project override**

The compiler parses frontmatter, requires `id`, `kind: assignment-routing`, `role: assignment-routing`, a valid scope, and body instructions. It emits a single `choose` LLM node followed by `done`; the node prompt includes the markdown body plus this fixed contract:

```text
Return one JSON object with a decisions array. Include exactly one decision for
every input task. Each decision may contain only task_id, input_hash,
intelligence_class, provider, and reason. Never choose profile_id, workspace,
lifecycle, pool, agent, or session. Use only options supplied in the event.
```

Update the manager's frontmatter dispatch so pipelines and assignment playbooks compile synchronously and every other kind retains the existing compiler-task path. Add the nullable project column across model/table/query/API/edit command. Validate an explicit override by kind, role, enabled state, and matching project scope; never fall back when it is invalid.

- [ ] **Step 4: Run compiler, project, and vault tests**

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/test_assignment_playbook_compiler.py tests/test_playbook_compiler_scope.py tests/test_vault.py tests/test_project_commands.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the playbook slice**

```bash
git add src/playbooks src/prompts/default_playbooks/default-assignment-routing.md src/vault.py src/models.py src/database src/api src/commands/project_commands.py migrations tests
git commit -m "feat: add assignment routing playbooks"
```

### Task 3: Run bounded assignment batches without agent sessions

**Files:**
- Create: `src/orchestrator/assignment_routing.py`
- Create: `tests/test_assignment_routing_coordinator.py`
- Modify: `src/assignment_routing.py`
- Modify: `src/playbooks/runner.py`
- Modify: `src/orchestrator/core.py`
- Modify: `tests/test_playbook_runner.py`

**Interfaces:**
- Produces: `build_assignment_options(project_id, profiles, agents, harness_registry, intelligence_classes, usage_snapshot) -> tuple[AssignmentOption, ...]`.
- Produces: `validate_assignment_response(response: str, candidates: Sequence[AssignmentCandidate], options: Sequence[AssignmentOption]) -> list[AssignmentDecision]`.
- Produces: `AssignmentRoutingCoordinator.reconcile() -> dict[str, EffectiveAssignmentRoute]` and `AssignmentRoutingCoordinator.routes_for(tasks: Sequence[Task]) -> dict[str, EffectiveAssignmentRoute]`.
- Extends: `PlaybookRunner(..., sync_task_projection: bool = True, tool_overrides: Sequence[str] | None = None)`.
- Consumes: Task 1 route methods and Task 2 `select_assignment_playbook`.

- [ ] **Step 1: Write failing runner-isolation, validation, batching, and stale-commit tests**

```python
@pytest.mark.asyncio
async def test_assignment_runner_has_no_tools_or_projected_task(runner_factory, db):
    runner = runner_factory(sync_task_projection=False, tool_overrides=[])
    await runner.run()
    assert runner.llm.calls[0].tools == []
    assert not [t for t in await db.list_tasks() if t.dedup_key.startswith("playbook-run:")]

@pytest.mark.asyncio
async def test_coordinator_batches_ready_tasks_once(coordinator, ready_tasks, llm):
    await coordinator.reconcile()
    assert len(llm.calls) == 1
    assert len(llm.calls[0].event["tasks"]) == len(ready_tasks)
    assert all(await coordinator.db.get_task_assignment_route(t.id) for t in ready_tasks)

@pytest.mark.asyncio
async def test_edit_during_llm_skips_only_edited_task(coordinator, tasks, llm):
    llm.before_response = lambda: coordinator.db.update_task(tasks[0].id, title="changed")
    await coordinator.reconcile()
    assert await coordinator.db.get_task_assignment_route(tasks[0].id) is None
    assert await coordinator.db.get_task_assignment_route(tasks[1].id) is not None
```

- [ ] **Step 2: Run focused tests and verify they fail**

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/test_playbook_runner.py tests/test_assignment_routing_coordinator.py -q`

Expected: FAIL because the runner always exposes its normal tools and syncs a run task, and no coordinator exists.

- [ ] **Step 3: Implement the option catalog, exact response validator, and coordinator**

The catalog includes only enabled ordinary worker routes. A task-lifecycle profile contributes every configured class with a model for its provider unless fixed worker settings conflict; a pool profile contributes only the class its long-running session actually launches. Busy workers count as compatible capacity. Sort the normalized options before hashing.

The coordinator performs this sequence:

```python
async def reconcile(self):
    candidates = await self._eligible_candidates()
    for project, batch in self._project_batches(candidates, limit=25):
        playbook = select_assignment_playbook(self.playbook_manager, project)
        options = await self._build_options(project.id)
        event_id = await self._next_attempt_id(project, playbook, batch, options)
        result = await PlaybookRunner(
            ..., playbook=playbook, event_id=event_id,
            sync_task_projection=False, tool_overrides=[],
        ).run()
        decisions = validate_assignment_response(result.final_response, batch, options)
        await self._commit_fresh_decisions(project, playbook, options, decisions)
```

Candidates are READY and unblocked, or blocked only by open routing gates; holds, dependency blockers, assignments, terminal tasks, and plan subtasks are excluded. Explicit classes resolve their routing gates without a model call. The batch key hashes project, playbook/version, ordered input hashes, and option hash; attempt IDs use the existing unique playbook-run event key. Catch duplicate insertion, observe the winning run, and advance the ordinal only after a terminal failed or invalid attempt. Keep one in-process run per project and exponential retry state capped at 300 seconds.

Commit in one immediate transaction after reloading each task. Recompute input hash, task revision, playbook/version, project, assignment state, and option hash; skip stale tasks individually and upsert unchanged decisions together. Resolve each committed task's routing gates after commit and record the playbook run ID in the resolution reason.

- [ ] **Step 4: Wire reconciliation before `_schedule()` and run coordinator tests**

Replace the normal-cycle call to `_reconcile_triage_tasks()` with `await self.assignment_routing.reconcile()`. Keep the legacy mixin/module for historical compatibility, but do not invoke it for automatic assignment.

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/test_assignment_routing_coordinator.py tests/test_playbook_runner.py tests/test_orchestrator.py -q`

Expected: PASS, including one LLM call for a multi-task project and no call for explicit tasks.

- [ ] **Step 5: Commit the coordinator slice**

```bash
git add src/assignment_routing.py src/orchestrator/assignment_routing.py src/orchestrator/core.py src/playbooks/runner.py tests
git commit -m "feat: route assignment classes in playbook batches"
```

### Task 4: Require effective routes in push scheduling and launch

**Files:**
- Modify: `src/scheduler.py`
- Modify: `src/agents/routing.py`
- Modify: `src/orchestrator/core.py`
- Modify: `src/orchestrator/agent_reconciler.py`
- Modify: `src/orchestrator/execution.py`
- Modify: `tests/test_scheduler.py`
- Modify: `tests/test_agent_reconciler.py`
- Create or modify: focused execution/prelaunch tests

**Interfaces:**
- Extends: `SchedulerState.assignment_routes: Mapping[str, EffectiveAssignmentRoute] | None`.
- Extends: `task_agent_mismatch(..., required_provider: str | None = None) -> str | None`.
- Extends: `AgentReconciler.reconcile(..., ready_tasks: Sequence[Task] | None = None)` so reconciliation uses the already filtered, class-hydrated task snapshot.
- Consumes: `AssignmentRoutingCoordinator.routes_for()` and `dataclasses.replace(task, intelligence_class=route.intelligence_class)`.

- [ ] **Step 1: Write failing push-path tests**

```python
def test_scheduler_skips_unclassified_task_without_effective_route(state):
    state.tasks[0].intelligence_class = None
    state.assignment_routes = {}
    assert Scheduler.schedule(state) == []

def test_scheduler_keeps_agent_choice_algorithmic(state, playbook_route):
    state.assignment_routes = {state.tasks[0].id: playbook_route}
    actions = Scheduler.schedule(state)
    assert actions[0].agent_id == state.agents[0].id

def test_provider_route_filters_incompatible_agent(state, openai_route):
    state.assignment_routes = {state.tasks[0].id: openai_route}
    assert all(action.agent_id != "anthropic-agent" for action in Scheduler.schedule(state))
```

Add async tests proving the reconciler does not create agents for unrouted tasks and the pre-launch recheck rejects a route that became stale after scheduling.

- [ ] **Step 2: Run push-path tests and verify they fail**

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/test_scheduler.py tests/test_agent_reconciler.py tests/test_orchestrator_constraints.py -q`

Expected: FAIL because scheduling and pre-launch still infer a profile default when the task class is absent.

- [ ] **Step 3: Hydrate routed tasks once, then reuse existing matching**

At the start of `_schedule()`, load fresh effective routes for the task snapshot. Drop READY candidates with neither an explicit nor a fresh playbook route, replace each retained task's class with the effective class, and pass the retained snapshot to both `AgentReconciler` and `SchedulerState`. Add `required_provider` to `task_agent_mismatch`; infer the candidate's provider from its resolved harness and return a mismatch when a non-null route provider differs.

Remove the `task_profile.default_class` fallback from the task side of `task_agent_mismatch`:

```python
required_class = _value(task, "intelligence_class")
```

Keep worker/profile defaults as compatibility constraints. Before reserving and again immediately before session launch, reload the task and route and repeat freshness/provider validation. Pass a class-hydrated task to `SessionSpecBuilder` and `resolve_launch_settings`, so the selected tier controls the actual launch.

- [ ] **Step 4: Run scheduler, reconciler, execution, and session-spec tests**

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/test_scheduler.py tests/test_agent_reconciler.py tests/test_orchestrator_constraints.py tests/test_session_spec.py tests/test_launch_configuration.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the push-path slice**

```bash
git add src/scheduler.py src/agents/routing.py src/orchestrator tests
git commit -m "feat: enforce assignment routes in push scheduling"
```

### Task 5: Require the same effective route in pool claims

**Files:**
- Modify: `src/database/queries/claim_queries.py`
- Modify: `src/commands/claim_commands.py`
- Modify: `src/orchestrator/pools.py` if the existing pool demand snapshot needs the routed class
- Modify: `tests/test_claim_queries.py`
- Modify: `tests/test_claim_commands.py`
- Modify: `tests/test_pool_reconciler.py`
- Modify: `tests/perf/test_claim_statements.py`

**Interfaces:**
- Extends: `select_ready_for_profile(..., intelligence_class: str, llm_provider: str | None, options_hash: str, conn) -> Task | None`.
- Changes: `_pool_claim_routing(session, profile) -> tuple[str, str | None]`, with no `allow_unclassified` result.
- Consumes: `task_assignment_routes.task_updated_at`, `tasks.updated_at`, and the current project option hash supplied by the same catalog builder.

- [ ] **Step 1: Write failing claim tests for missing, stale, and provider-constrained routes**

```python
@pytest.mark.asyncio
async def test_pool_claim_skips_unclassified_task_without_route(db, session):
    claimed = await claim_next(db, session)
    assert claimed is None

@pytest.mark.asyncio
async def test_pool_claim_accepts_fresh_matching_route(db, session, route):
    await save(route)
    assert (await claim_next(db, session)).id == route.task_id

@pytest.mark.asyncio
async def test_pool_claim_rejects_wrong_provider(db, anthropic_session, openai_route):
    await save(openai_route)
    assert await claim_next(db, anthropic_session) is None
```

- [ ] **Step 2: Run claim tests and verify they fail**

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/test_claim_queries.py tests/test_claim_commands.py tests/test_pool_reconciler.py -q`

Expected: FAIL because the claim query currently allows unclassified tasks and does not join derived routes.

- [ ] **Step 3: Add one joined effective-route predicate to the claim query**

Left-join `task_assignment_routes`. The SQL effective class is `tasks.intelligence_class` when nonempty, otherwise the derived class only when `task_updated_at == tasks.updated_at` and `options_hash` matches. Require that expression to equal the live session class. For derived routes, require nullable provider to match `SessionRecord.llm_provider`; explicit task classes have no provider pin. Remove `allow_unclassified` from commands and tests.

Use the existing immediate transaction and selection statement so route enforcement does not add a per-claim query. If pool demand is class-specific, hydrate its task snapshot with effective routes rather than creating a second fallback.

- [ ] **Step 4: Run functional and statement-budget comparisons**

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/test_claim_queries.py tests/test_claim_commands.py tests/test_pool_reconciler.py -q`

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/perf/test_claim_statements.py::TestClaimStatementBudgets::test_claim_happy_path_statement_budget[sqlite] -q`

Expected: functional tests PASS; the statement count must not increase from the recorded baseline of 24 because route enforcement is part of the existing SELECT.

- [ ] **Step 5: Commit the pool-claim slice**

```bash
git add src/database/queries/claim_queries.py src/commands/claim_commands.py src/orchestrator/pools.py tests
git commit -m "feat: enforce assignment routes in pool claims"
```

### Task 6: Cut over the default pipeline and expose route diagnostics

**Files:**
- Modify: `src/prompts/default_playbooks/default-pipeline.md`
- Modify: `src/orchestrator/core.py`
- Modify: `src/commands/task_commands.py`
- Modify: `src/explain.py`
- Modify: task detail notification/API builders that expose scheduling state
- Modify: `tests/test_default_pipeline.py`
- Modify: `tests/test_control_plane_e2e.py`
- Modify: `tests/test_explain.py`
- Create: `tests/test_assignment_routing_e2e.py`

**Interfaces:**
- Produces diagnostic codes `awaiting_intelligence_route`, `assignment_playbook_running`, `assignment_playbook_unavailable`, `assignment_route_retry`, `route_waiting_for_compatible_agent`, and `assignment_route_stale`.
- Produces an `assignment_route` detail object with source, class, provider, reason, playbook/version, run ID, and freshness.
- Consumes the same `resolve_effective_route()` result used by scheduling and claims.

- [ ] **Step 1: Write failing pipeline, diagnostic, and end-to-end tests**

```python
def test_default_pipeline_has_no_legacy_assignment_rules(compiled_default_pipeline):
    ids = {rule.id for rule in compiled_default_pipeline.rules}
    assert "task-created-routing" not in ids
    assert "worker-filed-triage" not in ids

@pytest.mark.asyncio
async def test_explain_unrouted_task_reports_awaiting_route(handler, task):
    result = await handler.execute("explain_task", {"task_id": task.id})
    assert "awaiting_intelligence_route" in result["reason_codes"]

@pytest.mark.asyncio
async def test_ready_task_routes_then_existing_scheduler_selects_agent(system):
    await system.run_one_cycle()
    route = await system.db.get_task_assignment_route(system.task.id)
    assert route.intelligence_class == "fast-low"
    assert (await system.db.get_task(system.task.id)).assigned_agent_id == system.worker.id
```

- [ ] **Step 2: Run cutover tests and verify they fail**

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/test_default_pipeline.py tests/test_control_plane_e2e.py tests/test_explain.py tests/test_assignment_routing_e2e.py -q`

Expected: FAIL while legacy routing rules remain and explanations lack assignment-route state.

- [ ] **Step 3: Remove only the two legacy rules and add diagnostics**

Delete `task-created-routing` and `worker-filed-triage` from both the prose and compiled JSON in the bundled `default-pipeline.md`. Do not remove review, spec-ingest, proposal, gate, or completion rules. Remove stale orchestrator documentation that claims orchestration makes zero LLM calls, replacing it with the precise exception for assignment routing.

Extend task explain/detail with one route object and one actionable reason code. Read coordinator in-flight/backoff state when available; otherwise distinguish missing, stale, unavailable override, and valid route waiting for compatible capacity. Do not create a new dashboard subsystem.

- [ ] **Step 4: Run focused cutover and end-to-end tests**

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/test_default_pipeline.py tests/test_default_pipeline_spec_and_proposal.py tests/test_control_plane_e2e.py tests/test_explain.py tests/test_assignment_routing_e2e.py -q`

Expected: PASS and unrelated default-pipeline rule IDs remain unchanged.

- [ ] **Step 5: Commit the cutover slice**

```bash
git add src/prompts/default_playbooks/default-pipeline.md src/orchestrator/core.py src/commands/task_commands.py src/explain.py src/notifications src/api tests
git commit -m "feat: cut assignment over to playbook routing"
```

### Task 7: Verify upgrade, concurrency, and no-fallback invariants

**Files:**
- Modify: tests created in Tasks 1-6 where gaps are found
- Modify: `docs/superpowers/specs/2026-08-31-playbook-intelligence-routing-design.md` only if implementation reveals a wording mismatch inside the approved boundary

**Interfaces:**
- Consumes all preceding public interfaces; produces no new runtime API.

- [ ] **Step 1: Add any missing acceptance tests discovered by the coverage review**

The final integration matrix must directly assert: explicit bypass; no push/claim/launch without a route; system default and project override isolation; one call per batch; nullable and pinned provider behavior; algorithmic profile/agent selection; stale edit rejection; invalid response retry without fallback; busy-capacity stability; catalog-change reroute; duplicate event/run protection; restart reconciliation; and absence of every ordinary-task profile-default class fallback.

```python
def test_missing_task_class_never_uses_profile_default(state):
    state.tasks[0].intelligence_class = None
    state.tasks[0].profile_id = "worker-deep"
    state.assignment_routes = {}
    assert Scheduler.schedule(state) == []
```

- [ ] **Step 2: Run the complete focused assignment suite**

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/test_assignment_routing.py tests/test_assignment_route_queries.py tests/test_assignment_playbook_compiler.py tests/test_assignment_routing_coordinator.py tests/test_assignment_routing_e2e.py tests/test_scheduler.py tests/test_agent_reconciler.py tests/test_claim_queries.py tests/test_claim_commands.py tests/test_pool_reconciler.py tests/test_playbook_runner.py tests/test_default_pipeline.py tests/test_default_pipeline_spec_and_proposal.py tests/test_control_plane_e2e.py tests/test_explain.py -q`

Expected: PASS.

- [ ] **Step 3: Run formatting, lint, type, migration, and repository tests**

Discover the repository's configured commands from `pyproject.toml`/CI and run those exact checks. Run Alembic upgrade against fresh SQLite and PostgreSQL test databases when the repository's integration environment supplies them. Then run `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest -x -q` and compare the first result with the recorded baseline failure.

- [ ] **Step 4: Search for forbidden ordinary-task fallback paths**

Run: `rg -n "intelligence_class.*or.*default_class|default_class.*intelligence_class|allow_unclassified|task-created-routing|worker-filed-triage" src tests`

Expected: no active ordinary scheduling, claim, reconciliation, or launch path can infer a missing task class; remaining matches are worker compatibility, tests, migration history, or intentionally preserved legacy code.

- [ ] **Step 5: Review the diff and commit verification fixes**

Run: `git diff --check && git status --short && git log --oneline --decorate -8`

Expected: no whitespace errors or untracked implementation files. Commit any test or documentation corrections with:

```bash
git add src tests docs migrations scripts
git commit -m "test: verify playbook assignment routing"
```

