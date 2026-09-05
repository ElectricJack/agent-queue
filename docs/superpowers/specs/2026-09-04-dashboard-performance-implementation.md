# Dashboard Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the dashboard and the orchestrator cycle stay fast with 5,000–10,000 tasks by removing the N+1 query paths, taking layout re-flow out of the cycle's critical path, and rendering the task list virtually.

**Architecture:** Every hot path found in the investigation is an N+1 loop over tasks or a whole-graph load per call. Each task below replaces one such loop with a set-based query (one `IN (...)` statement, or one join) behind a new `Database` method, and locks the result in with a statement-count test that is independent of task count. Client work is limited to not refetching what did not change and not rendering rows that are off screen.

**Tech Stack:** Python 3.12, SQLAlchemy Core (async, asyncpg + aiosqlite), FastAPI, Alembic, pytest-asyncio (auto mode); React 19, TanStack Query 5, `@tanstack/react-virtual`, vitest.

**Spec:** `docs/superpowers/specs/2026-09-04-dashboard-performance-investigation.md` — measurements, statement counts and the ranked findings this plan implements. Read it first; every task below cites the finding it closes.

## Global Constraints

- Statement counts are the contract. Every backend task adds a test that counts statements with a `before_cursor_execute` listener (pattern: `tests/perf/test_hierarchy_statements.py:32`) and asserts a bound that does **not** grow with task count.
- Migrations must work on both SQLite and PostgreSQL; `CheckConstraint`s are named; `server_default` takes bare values (CLAUDE.md "Database Migrations").
- Never run `alembic upgrade`/`stamp` against the operator's database from a worktree slot (CLAUDE.md "Never migrate the operator's database"). Tests build their own databases.
- Run only focused tests during a task: `aq test tests/test_<area>.py`. One area-wide run at the end of each workstream. Never bare `pytest tests/`.
- Every `Database` method added to a query mixin is also declared on the `Database` protocol in `src/database/base.py` (see line 259 for the dependency block, `get_gate_waiters` nearby for gates).
- Commands stay `{"success": bool, ...}`; API routers keep their existing response models (`src/api/models/graph.py`) — no wire-format change in this plan.
- Dashboard: `npm -w dashboard run test`, `npm -w dashboard run typecheck`, `npm -w dashboard run lint` must pass after each dashboard task. Dependencies are installed from the repo root (`npm install <pkg> -w dashboard`) because `package.json` declares `workspaces: ["packages/aq-ts-client", "dashboard"]`.
- ruff on changed files only: `ruff check <paths>` (line-length 100, py312).
- Benchmark before/after with the scratchpad scripts against the retained `aq_perfprobe` database (`BENCH_REUSE=1 .venv/bin/python <scratchpad>/bench.py`); the investigation's numbers are the baseline.

---

## Workstream A — the graph endpoint (finding §1)

### Task A1: Set-based edge and gate-waiter reads for one project

**Files:**
- Modify: `src/database/queries/dependency_queries.py` (add after `get_typed_dependencies_detailed`, line 212)
- Modify: `src/database/queries/gate_queries.py` (add after `get_gate_waiters`, line 397)
- Modify: `src/database/base.py:259` (protocol declarations)
- Test: `tests/test_dependency_queries.py`, `tests/test_api_graph.py`

**Interfaces:**
- Produces: `Database.list_project_edges(project_id: str) -> list[dict]` — every `task_dependencies` row whose `task_id` belongs to `project_id`, as `{"task_id", "depends_on_task_id", "dep_type", "description"}`, ordered by `(task_id, dep_type, depends_on_task_id)`. One statement.
- Produces: `Database.list_gate_waiters_for_project(project_id: str) -> dict[str, list[str]]` — `gate_id -> sorted task ids` for every gate in the project (gates with no waiters are absent). One statement.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dependency_queries.py`. Its `db` fixture (line 18) creates one project whose id is the module constant `PROJECT`; `Project`, `Task`, `TaskStatus` and `DepType` are already imported (line 12):

```python
async def test_list_project_edges_returns_typed_rows_for_one_project(db):
    await db.create_project(Project(id="p2", name="P2"))
    for tid, pid in (("a", PROJECT), ("b", PROJECT), ("c", "p2")):
        await db.create_task(Task(id=tid, project_id=pid, title=tid, description=""))
    await db.add_dependency("b", "a", description="needs a")
    await db.add_dependency("c", "a")  # cross-project edge, from p2

    rows = await db.list_project_edges(PROJECT)

    assert rows == [
        {"task_id": "b", "depends_on_task_id": "a", "dep_type": "blocks", "description": "needs a"},
    ]
    assert await db.list_project_edges("p2") == [
        {"task_id": "c", "depends_on_task_id": "a", "dep_type": "blocks", "description": None},
    ]
```

Append to `tests/test_api_graph.py` (fixture `db` there creates project `p1`):

```python
async def test_list_gate_waiters_for_project_groups_waiters_by_gate(db):
    await db.create_task(Task(id="t1", project_id="p1", title="One", description=""))
    await db.create_task(Task(id="t2", project_id="p1", title="Two", description=""))
    g1, _ = await db.create_gate(
        project_id="p1", gate_type="human", title="review", waiter_task_ids=["t2", "t1"]
    )
    g2, _ = await db.create_gate(project_id="p1", gate_type="timer", title="wait", await_id="x")

    waiters = await db.list_gate_waiters_for_project("p1")

    assert waiters == {g1: ["t1", "t2"]}
    assert g2 not in waiters
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `aq test tests/test_dependency_queries.py tests/test_api_graph.py -k "list_project_edges or list_gate_waiters_for_project" -v`
Expected: FAIL with `AttributeError: 'Database' object has no attribute 'list_project_edges'` (and the same for `list_gate_waiters_for_project`).

- [ ] **Step 3: Implement the two methods**

In `src/database/queries/dependency_queries.py`, after `get_typed_dependencies_detailed` (ends line 212):

```python
    async def list_project_edges(self, project_id: str) -> list[dict]:
        """Every outgoing edge of every task in *project_id*, in one statement.

        Feeds the aggregate graph endpoint, which used to call
        ``get_typed_dependencies_detailed`` once per task (N+1: 5,600
        statements and 11 s at 5,600 tasks).  Cross-project edges are
        included when their *from* task is in this project — the same set
        the per-task loop produced.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(
                    task_dependencies.c.task_id,
                    task_dependencies.c.depends_on_task_id,
                    task_dependencies.c.dep_type,
                    task_dependencies.c.description,
                )
                .select_from(
                    task_dependencies.join(tasks, tasks.c.id == task_dependencies.c.task_id)
                )
                .where(tasks.c.project_id == project_id)
                .order_by(
                    task_dependencies.c.task_id.asc(),
                    task_dependencies.c.dep_type.asc(),
                    task_dependencies.c.depends_on_task_id.asc(),
                )
            )
            return [
                {
                    "task_id": row[0],
                    "depends_on_task_id": row[1],
                    "dep_type": row[2],
                    "description": row[3],
                }
                for row in result.fetchall()
            ]
```

`tasks` is already imported in that module (it is used by `are_dependencies_met`); confirm with `grep -n "^from src.database.tables import" src/database/queries/dependency_queries.py` and add `tasks` to the import if missing.

In `src/database/queries/gate_queries.py`, after `get_gate_waiters` (ends line 397):

```python
    async def list_gate_waiters_for_project(self, project_id: str) -> dict[str, list[str]]:
        """``gate_id -> sorted waiter task ids`` for every gate in *project_id*.

        One statement for the whole project; the graph endpoint used to ask
        ``get_gate_waiters`` once per gate.
        """
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(task_gates.c.gate_id, task_gates.c.task_id)
                    .select_from(task_gates.join(gates, gates.c.id == task_gates.c.gate_id))
                    .where(gates.c.project_id == project_id)
                    .order_by(task_gates.c.gate_id.asc(), task_gates.c.task_id.asc())
                )
            ).fetchall()
        out: dict[str, list[str]] = {}
        for gate_id, task_id in rows:
            out.setdefault(gate_id, []).append(task_id)
        return out
```

`gates` and `task_gates` are already imported there (used by `list_gates`/`get_gate_waiters`).

In `src/database/base.py`, next to line 259:

```python
    async def list_project_edges(self, project_id: str) -> list[dict]: ...
```

and next to the existing `get_gate_waiters` declaration:

```python
    async def list_gate_waiters_for_project(self, project_id: str) -> dict[str, list[str]]: ...
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `aq test tests/test_dependency_queries.py tests/test_api_graph.py -k "list_project_edges or list_gate_waiters_for_project" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/database/queries/dependency_queries.py src/database/queries/gate_queries.py src/database/base.py tests/test_dependency_queries.py tests/test_api_graph.py
git commit -m "feat(db): set-based project edge and gate-waiter reads"
```

### Task A2: Narrow task projection for graph nodes

**Files:**
- Modify: `src/database/queries/task_queries.py` (add after `list_tasks`, line 288)
- Modify: `src/database/base.py:106` (protocol)
- Test: `tests/test_api_graph.py`

**Interfaces:**
- Produces: `Database.list_graph_task_rows(project_id: str) -> list[dict]` — one statement selecting only `id, title, status, priority, is_blocked, profile_id, intelligence_class, assigned_agent_id, branch_name, pr_url, dedup_key`, ordered by `(priority, created_at)` like `list_tasks`. `is_blocked` is returned as `bool`. No `description`, no JSON decoding, no `Task` hydration.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_graph.py`:

```python
async def test_list_graph_task_rows_is_a_narrow_projection(db):
    await db.create_task(Task(
        id="t1", project_id="p1", title="One", description="a very long description",
        priority=10, dedup_key="playbook-run:run-1",
    ))
    await db.create_task(Task(id="t2", project_id="p1", title="Two", description="", priority=5))

    rows = await db.list_graph_task_rows("p1")

    assert [r["id"] for r in rows] == ["t2", "t1"]  # priority asc
    assert set(rows[0]) == {
        "id", "title", "status", "priority", "is_blocked", "profile_id", "intelligence_class",
        "assigned_agent_id", "branch_name", "pr_url", "dedup_key",
    }
    assert rows[1]["dedup_key"] == "playbook-run:run-1"
    assert rows[1]["is_blocked"] is False
    assert rows[1]["status"] == "DEFINED"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `aq test tests/test_api_graph.py -k list_graph_task_rows -v`
Expected: FAIL with `AttributeError: ... 'list_graph_task_rows'`.

- [ ] **Step 3: Implement**

In `src/database/queries/task_queries.py`, after `list_tasks` (line 288):

```python
    _GRAPH_NODE_COLUMNS = (
        "id", "title", "status", "priority", "is_blocked", "profile_id",
        "intelligence_class", "assigned_agent_id", "branch_name", "pr_url", "dedup_key",
    )

    async def list_graph_task_rows(self, project_id: str) -> list[dict]:
        """The graph endpoint's node fields for every task in *project_id*.

        A narrow projection: ``list_tasks`` selects every column (the
        description and the JSON blobs ride along, ~1 KB per row) and
        hydrates a ``Task`` per row — 75 ms against 9 ms for this select at
        4,600 rows.  ``is_blocked`` is normalised to ``bool`` here so the
        caller never sees the 0/1 storage form.
        """
        cols = [getattr(tasks.c, name) for name in self._GRAPH_NODE_COLUMNS]
        stmt = (
            select(*cols)
            .where(tasks.c.project_id == project_id)
            .order_by(tasks.c.priority.asc(), tasks.c.created_at.asc())
        )
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            out = []
            for row in result.mappings().fetchall():
                d = dict(row)
                d["is_blocked"] = bool(d["is_blocked"])
                out.append(d)
            return out
```

In `src/database/base.py`, after the `list_tasks` declaration (line 106 block):

```python
    async def list_graph_task_rows(self, project_id: str) -> list[dict]: ...
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `aq test tests/test_api_graph.py -k list_graph_task_rows -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/database/queries/task_queries.py src/database/base.py tests/test_api_graph.py
git commit -m "feat(db): narrow graph-node projection for tasks"
```

### Task A3: Rewrite `GET /api/projects/{pid}/graph` on the batched reads

**Files:**
- Modify: `src/api/graph.py:34-101`
- Test: `tests/test_api_graph.py`

**Interfaces:**
- Consumes: `list_graph_task_rows`, `list_project_edges`, `list_gate_waiters_for_project` (Tasks A1, A2), plus existing `db.get_project`, `db.list_gates(project_id=...)`, `db.list_agents()`.
- Produces: same `ProjectGraphResponse` wire shape as today. Statement bound: **6** regardless of task count (project, tasks, edges, gates, waiters, agents).

- [ ] **Step 1: Write the failing statement-count test**

Append to `tests/test_api_graph.py`:

```python
from sqlalchemy import event


async def test_graph_endpoint_statement_count_does_not_grow_with_tasks(db, client_factory):
    # 40 tasks in a chain, one gate over half of them.
    ids = [f"t{i}" for i in range(40)]
    for tid in ids:
        await db.create_task(Task(id=tid, project_id="p1", title=tid, description=""))
    for a, b in zip(ids[1:], ids):
        await db.add_dependency(a, b)
    await db.create_gate(project_id="p1", gate_type="human", title="r", waiter_task_ids=ids[:20])

    counter = {"n": 0}

    def _hook(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(db._engine.sync_engine, "before_cursor_execute", _hook)
    try:
        async with client_factory() as ac:
            r = await ac.get("/api/projects/p1/graph")
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", _hook)

    assert r.status_code == 200
    body = r.json()
    assert len(body["tasks"]) == 40 and len(body["edges"]) == 39
    assert body["gates"][0]["task_ids"] == sorted(ids[:20])
    # project + tasks + edges + gates + waiters + agents
    assert counter["n"] <= 6, counter["n"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `aq test tests/test_api_graph.py -k statement_count -v`
Expected: FAIL on the final assertion with a count around 45 (one per task plus one per gate plus the fixed reads).

- [ ] **Step 3: Rewrite the endpoint body**

Replace the body of `get_project_graph` inside `build_graph_router` (`src/api/graph.py:34-101`) with:

```python
    async def get_project_graph(project_id: str) -> ProjectGraphResponse:
        project = await db.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"No project '{project_id}'")

        rows = await db.list_graph_task_rows(project_id)
        task_ids = {r["id"] for r in rows}

        # One statement for every outgoing edge of every task in the project
        # (cross-project edges land client-side when the peer project's graph
        # is also loaded, spec §9.2) — was one statement per task.
        edges = [
            GraphEdge(
                from_task_id=e["task_id"],
                to_task_id=e["depends_on_task_id"],
                dep_type=e["dep_type"],
                description=e["description"],
            )
            for e in await db.list_project_edges(project_id)
        ]

        waiters_by_gate = await db.list_gate_waiters_for_project(project_id)
        gates = [
            GraphGate(
                id=g["id"],
                gate_type=g["gate_type"],
                status=g["status"],
                task_ids=waiters_by_gate.get(g["id"], []),
            )
            for g in await db.list_gates(project_id=project_id)
        ]

        agents = [
            GraphAgent(
                id=a.id,
                name=a.name,
                profile_id=a.profile_id,
                current_task_id=a.current_task_id,
                session_id=getattr(a, "session_id", None),
            )
            for a in await db.list_agents()
            if a.current_task_id in task_ids
        ]

        def _run_id(dedup_key: str | None) -> str | None:
            if dedup_key and dedup_key.startswith("playbook-run:"):
                return dedup_key.removeprefix("playbook-run:")
            return None

        return ProjectGraphResponse(
            tasks=[
                GraphTaskNode(
                    id=r["id"],
                    title=r["title"],
                    status=r["status"],
                    priority=r["priority"],
                    is_blocked=r["is_blocked"],
                    profile_id=r["profile_id"],
                    intelligence_class=r["intelligence_class"],
                    assigned_agent_id=r["assigned_agent_id"],
                    branch_name=r["branch_name"],
                    pr_url=r["pr_url"],
                    playbook_run_id=_run_id(r["dedup_key"]),
                )
                for r in rows
            ],
            edges=edges,
            gates=gates,
            agents=agents,
        )
```

Update the module docstring's "Deviation from the plan's draft" paragraph (lines 6-14) to say the endpoint now reads edges with `list_project_edges` in one statement; the per-task helper description is stale.

- [ ] **Step 4: Run the graph API tests**

Run: `aq test tests/test_api_graph.py -v`
Expected: all PASS, including `test_tasks_edges_gates_agents_are_included` (existing wire-shape test) and the new statement-count test.

- [ ] **Step 5: Benchmark**

Run: `BENCH_REUSE=1 .venv/bin/python /tmp/claude-1000/-home-jkern-dev-agent-queue2/4f53f3a3-3a4b-4559-a0b6-1d1f10f8d691/scratchpad/bench.py 2>&1 | grep "GET /graph"`
Expected: both projects under 150 ms with `stmts/req 6` (baseline: 10–11.5 s, 5,000+ statements).

- [ ] **Step 6: Commit**

```bash
git add src/api/graph.py tests/test_api_graph.py
git commit -m "perf(api): batch the project graph endpoint into six statements"
```

---

## Workstream B — the promotion cascade (finding §2)

### Task B1: Batched typed-dependency and status reads

**Files:**
- Modify: `src/database/queries/dependency_queries.py` (after `get_typed_dependencies`, line 188)
- Modify: `src/database/queries/task_queries.py` (after `list_graph_task_rows` from A2)
- Modify: `src/database/base.py`
- Test: `tests/test_dependency_queries.py`

**Interfaces:**
- Produces: `Database.get_typed_dependencies_for_tasks(task_ids: list[str]) -> dict[str, list[tuple[str, str]]]` — `task_id -> [(depends_on_task_id, dep_type), ...]` ordered `(dep_type, depends_on_task_id)` exactly like `get_typed_dependencies`; every id in `task_ids` is a key (empty list when it has no edges). Chunked at 900 ids per statement (SQLite's parameter ceiling; same constant `_chunks` uses in `layout_queries.py:14`).
- Produces: `Database.get_task_statuses(task_ids: list[str]) -> dict[str, str]` — `id -> status value` for the ids that exist. Chunked the same way.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dependency_queries.py`:

```python
async def test_get_typed_dependencies_for_tasks_matches_the_per_task_reads(db):
    for tid in ("a", "b", "c", "lonely"):
        await db.create_task(Task(id=tid, project_id=PROJECT, title=tid, description=""))
    await db.add_dependency("c", "b")
    await db.add_dependency("c", "a", "waits-for")
    await db.add_dependency("b", "a")

    batched = await db.get_typed_dependencies_for_tasks(["a", "b", "c", "lonely"])

    for tid in ("a", "b", "c", "lonely"):
        assert batched[tid] == await db.get_typed_dependencies(tid), tid
    assert batched["lonely"] == []


async def test_get_task_statuses_returns_only_existing_ids(db):
    await db.create_task(Task(id="a", project_id=PROJECT, title="a", description=""))
    await db.create_task(
        Task(id="b", project_id=PROJECT, title="b", description="", status=TaskStatus.COMPLETED)
    )

    assert await db.get_task_statuses(["a", "b", "ghost"]) == {"a": "DEFINED", "b": "COMPLETED"}
    assert await db.get_task_statuses([]) == {}
```

`add_dependency(task_id, depends_on, dep_type=DepType.BLOCKS.value, *, description=None, conn=None)` (`dependency_queries.py:52-60`) takes the type as the third positional argument, as above.

- [ ] **Step 2: Run to verify failure**

Run: `aq test tests/test_dependency_queries.py -k "for_tasks or get_task_statuses" -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement**

In `src/database/queries/dependency_queries.py` after `get_typed_dependencies` (line 188):

```python
    async def get_typed_dependencies_for_tasks(
        self, task_ids: list[str]
    ) -> dict[str, list[tuple[str, str]]]:
        """``get_typed_dependencies`` for many tasks in ``ceil(n / 900)`` statements.

        The promotion cascade asks this every cycle for every DEFINED and
        BLOCKED task; one statement per task cost 9 s per 5 s cycle at 4,600
        DEFINED tasks.  Every requested id is a key so callers can index
        without a default.
        """
        out: dict[str, list[tuple[str, str]]] = {tid: [] for tid in task_ids}
        ids = sorted(out)
        if not ids:
            return out
        async with self._engine.begin() as conn:
            for i in range(0, len(ids), 900):
                chunk = ids[i : i + 900]
                result = await conn.execute(
                    select(
                        task_dependencies.c.task_id,
                        task_dependencies.c.depends_on_task_id,
                        task_dependencies.c.dep_type,
                    )
                    .where(task_dependencies.c.task_id.in_(chunk))
                    .order_by(
                        task_dependencies.c.task_id.asc(),
                        task_dependencies.c.dep_type.asc(),
                        task_dependencies.c.depends_on_task_id.asc(),
                    )
                )
                for tid, dep, typ in result.fetchall():
                    out[tid].append((dep, typ))
        return out
```

In `src/database/queries/task_queries.py` after `list_graph_task_rows`:

```python
    async def get_task_statuses(self, task_ids: list[str]) -> dict[str, str]:
        """``id -> status`` for the ids that exist, in ``ceil(n / 900)`` statements."""
        ids = sorted(set(task_ids))
        if not ids:
            return {}
        out: dict[str, str] = {}
        async with self._engine.begin() as conn:
            for i in range(0, len(ids), 900):
                chunk = ids[i : i + 900]
                rows = (
                    await conn.execute(
                        select(tasks.c.id, tasks.c.status).where(tasks.c.id.in_(chunk))
                    )
                ).fetchall()
                out.update({r[0]: r[1] for r in rows})
        return out
```

Protocol declarations in `src/database/base.py` next to `get_typed_dependencies_detailed` and `list_tasks` respectively:

```python
    async def get_typed_dependencies_for_tasks(
        self, task_ids: list[str]
    ) -> dict[str, list[tuple[str, str]]]: ...
    async def get_task_statuses(self, task_ids: list[str]) -> dict[str, str]: ...
```

- [ ] **Step 4: Run to verify pass**

Run: `aq test tests/test_dependency_queries.py -k "for_tasks or get_task_statuses" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/database/queries/dependency_queries.py src/database/queries/task_queries.py src/database/base.py tests/test_dependency_queries.py
git commit -m "feat(db): batched typed-dependency and status reads"
```

### Task B2: Make the legacy promotion scan set-based

**Files:**
- Modify: `src/orchestrator/monitoring.py:244-272` (`_check_defined_tasks` blocked filter) and `:353-430` (`_legacy_promotion_decisions`)
- Test: `tests/test_work_graph_cascade.py`

**Interfaces:**
- Consumes: `get_typed_dependencies_for_tasks`, `get_task_statuses` (B1), existing `task_ids_with_meta` (`task_queries.py:1455`), `BLOCKING_DEP_TYPES` from `src.models`.
- Produces: `_legacy_promotion_decisions(defined, blocked)` with the **same return value** as today for every input; statement bound **3** (edges, statuses, and nothing else) regardless of how many tasks are passed.

Semantics to preserve, from the current loop (`monitoring.py:384-429`):
1. A task with any edge of a type in `_LEGACY_UNKNOWN_DEP_TYPES`, or a `parent-child` edge that is not "plan child → its own parent", is **deferred**.
2. A plan child (`is_plan_subtask and parent_task_id`) whose parent is `IN_PROGRESS`: promoted with `"deps_met_plan_parent_active"` iff every *blocking* dep other than the parent is `COMPLETED`; otherwise nothing (falls through to `continue`).
3. Otherwise `deps` = blocking-type deps: none → DEFINED gets `"deps_met_no_deps"` (BLOCKED gets nothing); some → `"deps_met"` iff all `COMPLETED`. (`get_dependencies` and `are_dependencies_met` both default to `BLOCKING_DEP_TYPES` via `_dep_type_filter(None)`, `dependency_queries.py:19-22`.)

- [ ] **Step 1: Write the failing parity + statement-count test**

Append to `tests/test_work_graph_cascade.py` (fixture `orch`, helper `mktask` already exist at the top of the file):

```python
from sqlalchemy import event


class TestLegacyScanIsBatched:
    async def test_legacy_scan_uses_a_fixed_number_of_statements(self, orch):
        # 30 DEFINED tasks: a chain, a plan child, a waits-for edge, some lonely.
        ids = [await mktask(orch, f"t{i}") for i in range(30)]
        for a, b in zip(ids[1:10], ids[:9]):
            await orch.db.add_dependency(a, b)
        plan = await mktask(orch, "plan", status=TaskStatus.IN_PROGRESS)
        child = await mktask(orch, "child", is_plan_subtask=True, parent_task_id=plan)
        await orch.db.add_dependency(child, plan)
        await orch.db.add_dependency(ids[20], ids[21], "waits-for")
        defined = await orch.db.list_tasks(status=TaskStatus.DEFINED)
        blocked = await orch.db.list_tasks(status=TaskStatus.BLOCKED)

        counter = {"n": 0}

        def _hook(conn, cursor, statement, parameters, context, executemany):
            counter["n"] += 1

        event.listen(orch.db._engine.sync_engine, "before_cursor_execute", _hook)
        try:
            decisions, deferred = await orch._legacy_promotion_decisions(defined, blocked)
        finally:
            event.remove(orch.db._engine.sync_engine, "before_cursor_execute", _hook)

        assert counter["n"] <= 3, counter["n"]
        assert decisions[ids[0]] == "deps_met_no_deps"
        assert ids[1] not in decisions  # blocked on t0, which is DEFINED
        assert ids[20] in deferred
        assert decisions[child] == "deps_met_plan_parent_active"
```

`mktask` passes `**kw` straight to `Task(...)` (`tests/test_work_graph_cascade.py:39-43`), and `is_plan_subtask` / `parent_task_id` are `Task` fields (`src/models.py:447,459`). `add_dependency` takes the edge type as its third positional argument (`dependency_queries.py:52-56`). `"waits-for"` is one of `_LEGACY_UNKNOWN_DEP_TYPES` (check the constant at the top of `monitoring.py`), which is what makes `ids[20]` land in `deferred`.

- [ ] **Step 2: Run to verify failure**

Run: `aq test tests/test_work_graph_cascade.py -k legacy_scan_uses -v`
Expected: FAIL on `counter["n"] <= 3` (the current loop issues ~2 statements per task, well over 60).

- [ ] **Step 3: Rewrite `_legacy_promotion_decisions`**

Replace lines 382-429 of `src/orchestrator/monitoring.py` (the `decisions: dict[str, str] = {}` line through `return decisions, deferred`) with:

```python
        decisions: dict[str, str] = {}
        deferred: set[str] = set()
        candidates = [*defined, *blocked]
        if not candidates:
            return decisions, deferred

        # One statement for every candidate's edges, one for every status the
        # rules below read (dependency targets and plan parents).  The loop
        # used to issue two to four statements per task — 9 s per cycle at
        # 4,600 DEFINED tasks.
        edges_by_task = await self.db.get_typed_dependencies_for_tasks([t.id for t in candidates])
        status_ids: set[str] = set()
        for task in candidates:
            status_ids.update(dep for dep, _ in edges_by_task[task.id])
            if task.is_plan_subtask and task.parent_task_id:
                status_ids.add(task.parent_task_id)
        statuses = await self.db.get_task_statuses(sorted(status_ids))
        completed = TaskStatus.COMPLETED.value

        for task in candidates:
            typed_edges = edges_by_task[task.id]
            is_plan_child = bool(task.is_plan_subtask and task.parent_task_id)
            if any(
                dep_type in _LEGACY_UNKNOWN_DEP_TYPES
                or (
                    dep_type == DepType.PARENT_CHILD.value
                    and not (is_plan_child and target == task.parent_task_id)
                )
                for target, dep_type in typed_edges
            ):
                deferred.add(task.id)
                continue

            # Blocking edges only — the same set ``get_dependencies`` and
            # ``are_dependencies_met`` default to.
            blocking = {dep for dep, typ in typed_edges if typ in BLOCKING_DEP_TYPES}

            # Plan subtask special handling: the parent plan transitions to
            # IN_PROGRESS (not COMPLETED) when approved, so the plain
            # all-COMPLETED rule would block forever.  Treat the IN_PROGRESS
            # parent dep as satisfied and judge only the other deps.
            if is_plan_child and statuses.get(task.parent_task_id) == TaskStatus.IN_PROGRESS.value:
                non_parent = blocking - {task.parent_task_id}
                if all(statuses.get(dep) == completed for dep in non_parent):
                    decisions[task.id] = "deps_met_plan_parent_active"
                continue

            if not blocking:
                if task.status == TaskStatus.DEFINED:
                    # No dependencies — promote DEFINED to READY.  (BLOCKED
                    # tasks with no deps stay blocked — they were blocked for
                    # other reasons like verification failure.)
                    decisions[task.id] = "deps_met_no_deps"
            elif all(statuses.get(dep) == completed for dep in blocking):
                decisions[task.id] = "deps_met"
        return decisions, deferred
```

Add `BLOCKING_DEP_TYPES` to the `from src.models import ...` line at the top of `monitoring.py` (check the current import list with `grep -n "^from src.models import" src/orchestrator/monitoring.py`).

Note one deliberate equivalence: the old code called `get_task(dep_id)` and treated a **missing** dependency task as not met (`if not dep_task or ...`); `statuses.get(dep) == completed` is `False` for a missing id, so the batched form agrees.

- [ ] **Step 4: Replace the per-task `needs_attention` lookup**

In `_check_defined_tasks` (`monitoring.py:248-252`), replace:

```python
        blocked = [
            task for task in blocked
            if not await self.db.get_task_meta(task.id, "needs_attention")
        ]
```

with:

```python
        # One statement instead of one per BLOCKED task.  ``needs_attention``
        # is written as a non-empty code string, so "key present" and "value
        # truthy" coincide.
        attention = await self.db.task_ids_with_meta(
            [task.id for task in blocked], "needs_attention"
        )
        blocked = [task for task in blocked if task.id not in attention]
```

- [ ] **Step 5: Run the cascade suite**

Run: `aq test tests/test_work_graph_cascade.py tests/test_orchestrator.py -k "cascade or Shadow or legacy or defined or promot" -v`
Expected: all PASS, in particular `TestShadowParity::*` (legacy and projection still agree) and the new statement-count test.

- [ ] **Step 6: Benchmark**

Run: `BENCH_REUSE=1 .venv/bin/python <scratchpad>/bench.py 2>&1 | grep -A2 "promotion cascade"` — this line in the script still loops `get_typed_dependencies`; instead time the real method:

```bash
BENCH_REUSE=1 .venv/bin/python - <<'EOF'
import asyncio, sys, time
sys.path.insert(0, "/home/jkern/dev/agent-queue2")
from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
from src.models import TaskStatus
async def main():
    db = PostgreSQLDatabaseAdapter("postgresql://agent_queue:agent_queue_dev@localhost:5533/aq_perfprobe")
    await db.initialize()
    defined = await db.list_tasks(status=TaskStatus.DEFINED)
    t0 = time.perf_counter()
    edges = await db.get_typed_dependencies_for_tasks([t.id for t in defined])
    statuses = await db.get_task_statuses(sorted({d for es in edges.values() for d, _ in es}))
    print(f"{len(defined)} DEFINED: {(time.perf_counter()-t0)*1000:.0f} ms")
    await db.close()
asyncio.run(main())
EOF
```

Expected: well under 200 ms (baseline 9.1 s).

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/monitoring.py tests/test_work_graph_cascade.py
git commit -m "perf(orchestrator): batch the legacy promotion scan and the needs_attention filter"
```

---

## Workstream C — indexes and PostgreSQL settings (finding §8)

### Task C1: Status-leading task index and pattern-ops path index

**Files:**
- Modify: `src/database/tables.py:138-142` (tasks indexes) and `:244` (task_layouts path index)
- Create: `migrations/versions/<rev>_perf_indexes_status_and_path.py` (autogenerate, then hand-edit)
- Test: `tests/test_database.py` (existing migration smoke) plus `alembic check`

- [ ] **Step 1: Edit `tables.py`**

After line 142 (`Index("idx_tasks_ready_by_profile", ...)`) add:

```python
    # Status-only lists (``list_tasks(status=...)`` in the monitoring cycle,
    # ``aq task list --status``) had no index leading with status and
    # seq-scanned ``tasks`` as completed history grew.
    Index("idx_tasks_status_project", "status", "project_id"),
```

Replace line 244 (`Index("idx_task_layouts_path", "project_id", "variant", "path"),`) with:

```python
    # ``load_paths_by_prefixes`` / ``load_subtree_ids`` filter ``path LIKE
    # '/a/b/%'``.  On PostgreSQL a plain btree under a non-C collation cannot
    # serve a LIKE prefix, so the run recorded zero scans of this index;
    # ``text_pattern_ops`` makes the prefix scan index-driven.  SQLite
    # ignores the op class.
    Index(
        "idx_task_layouts_path",
        "project_id",
        "variant",
        "path",
        postgresql_ops={"path": "text_pattern_ops"},
    ),
```

- [ ] **Step 2: Generate the migration**

Run from the repo root with a throwaway SQLite database so the operator's database is never touched:

```bash
AGENT_QUEUE_DB_URL=sqlite:///$PWD/.tmp-migr.db .venv/bin/alembic upgrade head
AGENT_QUEUE_DB_URL=sqlite:///$PWD/.tmp-migr.db .venv/bin/alembic revision --autogenerate -m "perf indexes status and path"
rm -f .tmp-migr.db
```

(`migrations/env.py:33` reads `AGENT_QUEUE_DB_URL` first.) Autogenerate will emit `create_index("idx_tasks_status_project", ...)` and, because it does not compare op classes, **nothing** for `idx_task_layouts_path`. Hand-edit the new file's `upgrade()`/`downgrade()` to:

```python
def upgrade() -> None:
    op.create_index("idx_tasks_status_project", "tasks", ["status", "project_id"])
    op.drop_index("idx_task_layouts_path", table_name="task_layouts")
    op.create_index(
        "idx_task_layouts_path",
        "task_layouts",
        ["project_id", "variant", "path"],
        postgresql_ops={"path": "text_pattern_ops"},
    )


def downgrade() -> None:
    op.drop_index("idx_task_layouts_path", table_name="task_layouts")
    op.create_index("idx_task_layouts_path", "task_layouts", ["project_id", "variant", "path"])
    op.drop_index("idx_tasks_status_project", table_name="tasks")
```

Set `down_revision` to the current head (`.venv/bin/alembic heads` printed `e6a1b2c3d4f5` at the time of writing; use whatever it prints now) and write a docstring in the style of `migrations/versions/a5d2c0de0008_pending_event_resolution_reason.py` explaining the two indexes and the measured zero-scan finding.

- [ ] **Step 3: Verify both dialects**

```bash
AGENT_QUEUE_DB_URL=sqlite:///$PWD/.tmp-migr.db .venv/bin/alembic upgrade head && AGENT_QUEUE_DB_URL=sqlite:///$PWD/.tmp-migr.db .venv/bin/alembic check; rm -f .tmp-migr.db
aq test tests/test_database.py tests/test_migration_string_defaults.py tests/test_migration_boolean_defaults.py
```

Expected: `alembic check` reports no new operations; tests PASS. Then against the retained probe database (this is a throwaway database, not the operator's):

```bash
AGENT_QUEUE_DB_URL=postgresql://agent_queue:agent_queue_dev@localhost:5533/aq_perfprobe .venv/bin/alembic upgrade head
.venv/bin/python <scratchpad>/q.py "explain select task_id, path from task_layouts where project_id='perf' and variant='active' and path like '/epic0/%'"
```

Expected: the plan shows `Index Scan using idx_task_layouts_path` (or a Bitmap Index Scan on it) with an `Index Cond` containing `path ~>=~ '/epic0/'`.

- [ ] **Step 4: Commit**

```bash
git add src/database/tables.py migrations/versions/*perf_indexes_status_and_path.py
git commit -m "perf(db): status-leading task index and pattern-ops layout path index"
```

### Task C2: Raise `work_mem` and `shared_buffers` on the compose PostgreSQL

**Files:**
- Modify: `docker-compose.yml:4-18`
- Modify: `docs/guides/resource-gating.md` or the database playbook that documents the compose instance (find with `grep -rln "5533" docs/`) — one paragraph

- [ ] **Step 1: Add a `command:` to the postgres service**

After the `image: postgres:18-alpine` line add:

```yaml
    command:
      - postgres
      - -c
      - shared_buffers=512MB
      - -c
      - work_mem=32MB
      - -c
      - effective_cache_size=2GB
      - -c
      - shared_preload_libraries=pg_stat_statements
```

`work_mem=32MB` removes the disk spill seen on `list_tasks(project_id=...)` (`external merge Disk: 4552kB` under the 4 MB default). `pg_stat_statements` preloaded makes the next investigation a `SELECT` instead of a probe (the extension itself still needs `CREATE EXTENSION pg_stat_statements;` once, run by the operator).

- [ ] **Step 2: Document**

In the guide found in Step 0 add one paragraph: the compose instance carries non-default `shared_buffers`/`work_mem`/`effective_cache_size`; a production PostgreSQL should be tuned at least as far; `pg_stat_statements` is preloaded and enabled with `CREATE EXTENSION`.

- [ ] **Step 3: Verify**

```bash
docker compose config | grep -A9 "command:"
```

Expected: the nine arguments above. Do **not** restart the operator's container as part of this task; note in the commit message that the change takes effect on the next `docker compose up -d`.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml docs/
git commit -m "chore(postgres): tune work_mem/shared_buffers and preload pg_stat_statements"
```

---

## Workstream D — layout off the critical path (finding §3)

### Task D1: Run the layout step as a background task instead of inline in the cycle

**Files:**
- Modify: `src/orchestrator/layout_step.py:24-40`
- Modify: `src/orchestrator/core.py:2339-2343`
- Test: `tests/task_graph/test_layout_step.py`

**Interfaces:**
- Produces: `LayoutStepMixin.schedule_layout_step() -> asyncio.Task | None` — starts `_run_layout_step()` as an `asyncio.Task` when none is running and returns it; returns the running task (without starting another) when one is in flight. Attribute `_layout_bg: asyncio.Task | None`.
- Produces: `LayoutStepMixin.wait_for_layout_step() -> None` — awaits the in-flight task if any (tests and shutdown use it).
- `_run_layout_step()` keeps its signature; tests that call it directly are unaffected.

- [ ] **Step 1: Write the failing test**

Append to `tests/task_graph/test_layout_step.py`:

```python
import asyncio


async def test_schedule_layout_step_runs_in_the_background_and_does_not_overlap(orchestrator_factory):
    o = await orchestrator_factory()
    o.config.graph_layout.enabled = True
    o.config.graph_layout.incremental_debounce_ms = 0
    await o.db.create_project(Project(id="p1", name="P1"))
    await o.db.create_task(Task(id="a", project_id="p1", title="a", description=""))

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_step():
        started.set()
        await release.wait()

    with patch.object(type(o), "_run_layout_step", new=slow_step):
        first = o.schedule_layout_step()
        await started.wait()
        second = o.schedule_layout_step()
        assert second is first  # one in flight at a time
        release.set()
        await o.wait_for_layout_step()
        assert first.done()

    # After the in-flight task finishes, the next call starts a real step.
    third = o.schedule_layout_step()
    assert third is not first
    await o.wait_for_layout_step()
    assert (await o.db.get_layout_meta("p1", "all"))["node_count"] == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `aq test tests/task_graph/test_layout_step.py -k schedule_layout_step -v`
Expected: FAIL with `AttributeError: ... 'schedule_layout_step'`.

- [ ] **Step 3: Implement**

In `src/orchestrator/layout_step.py`, inside `LayoutStepMixin` before `_run_layout_step` (line 27), add:

```python
    _layout_bg: asyncio.Task | None

    def schedule_layout_step(self) -> asyncio.Task | None:
        """Start ``_run_layout_step`` in the background, at most one at a time.

        Layout is a projection of task state; folding a batch of dirty marks
        re-runs the engine over the dirty containers, which measured 0.5 s
        in a 5,600-task hierarchy and 1.4 s for one change in a 5,000-task
        flat project.  Awaiting that inline made every status change stretch
        the 5 s cycle, so the cycle now only *kicks* the step and moves on.
        Marks are durable: a step that is skipped because one is already
        running picks them up on the next cycle.
        """
        bg = getattr(self, "_layout_bg", None)
        if bg is not None and not bg.done():
            return bg
        if bg is not None:
            exc = bg.exception() if not bg.cancelled() else None
            if exc is not None:
                logger.warning("background layout step failed: %s", exc)
        self._layout_bg = asyncio.create_task(self._run_layout_step(), name="layout-step")
        return self._layout_bg

    async def wait_for_layout_step(self) -> None:
        """Await the in-flight background step, if any (tests, shutdown)."""
        bg = getattr(self, "_layout_bg", None)
        if bg is not None and not bg.done():
            await bg
```

Add `import asyncio` to the module imports.

In `src/orchestrator/core.py:2339-2343` replace:

```python
            try:
                await self._run_layout_step()
            except Exception as e:
                logger.warning("Layout step failed: %s", e)
```

with:

```python
            # Kicked, not awaited: the step re-runs the engine over dirty
            # containers and must not stretch the cycle (perf investigation
            # 2026-09-04 §3).  ``_run_layout_step`` never raises.
            self.schedule_layout_step()
```

In `Orchestrator.shutdown` (`core.py:2117`), directly after the existing `await self.wait_for_running_tasks(timeout=10)` (`core.py:2129`), add:

```python
        # A layout publish is one transaction; let an in-flight step land
        # rather than cancelling it mid-write.  Marks are durable either way.
        await self.wait_for_layout_step()
```

- [ ] **Step 4: Run the layout step tests**

Run: `aq test tests/task_graph/test_layout_step.py tests/test_orchestrator.py -k "layout" -v`
Expected: PASS. If `test_orchestrator.py` has a test asserting that `run_one_cycle` produced a layout synchronously, add `await o.wait_for_layout_step()` after the cycle call in that test.

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/layout_step.py src/orchestrator/core.py tests/task_graph/test_layout_step.py
git commit -m "perf(layout): run the layout step as a background task instead of inline in the cycle"
```

### Task D2: Aggregates-only fast path for status marks in the `all` variant

**Files:**
- Modify: `src/task_graph/layout/driver.py:478-524` (`_seed_queue`)
- Test: `tests/task_graph/test_layout_driver.py`

**Interfaces:**
- Consumes: dirty-mark reasons written by `transition_task` (`task_queries.py:935`): `"status.finished"` / `"status.reopened"`.
- Produces: in variant `all`, a mark with a `status.*` reason on a task that is **present, not a container, and already has a stored row** dirties no container: the batch only refreshes ancestor aggregates (`_refresh_aggregates` already walks `dirty_tasks`' ancestors). Every other case behaves exactly as today. The `active` variant is unchanged in this task (membership changes there move siblings).

Why this is sound: in `all` every task is present regardless of status (`_visible`, `driver.py:49-52`), so a leaf's status flip changes no box; only the ancestors' `agg_completed/agg_active/agg_running` counters change, and `_refresh_aggregates` recomputes those from the snapshot for every ancestor of every dirty task.

- [ ] **Step 1: Write the failing test**

Append to `tests/task_graph/test_layout_driver.py` (fixture `db`, helper `seed_epic` exist at the top):

```python
from sqlalchemy import event


async def test_status_flip_in_all_variant_updates_aggregates_without_relaying(db):
    kids = await seed_epic(db, n=4)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    before = await db.load_layout_rows("p1", "all", ["e", *kids])

    await db.transition_task(kids[1], TaskStatus.COMPLETED, force=True)  # marks "status.finished"

    statements: list[str] = []

    def _hook(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db._engine.sync_engine, "before_cursor_execute", _hook)
    try:
        versions = await drv.process_dirty("p1", min_age_seconds=0)
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", _hook)

    assert versions["all"] == 2
    after = await db.load_layout_rows("p1", "all", ["e", *kids])
    for k in kids:  # nobody moved
        assert (after[k].abs_x, after[k].abs_y, after[k].w, after[k].h) == (
            before[k].abs_x, before[k].abs_y, before[k].w, before[k].h)
    assert after["e"].agg_completed == 1 and after["e"].agg_active == 3
    # The engine pass reads a container's children with load_children_layout_rows;
    # a pure status flip must not trigger it for the `all` variant.
    assert not any("container_id = " in s and "task_layouts" in s for s in statements), statements
```

The last assertion keys on the SQL `load_children_layout_rows` emits (`layout_queries.py:387-404`: `WHERE ... task_layouts.container_id = ?`). The `active` variant's pass still re-lays in this task and legitimately issues that statement, so wrap the `process_dirty` call in `with patch("src.task_graph.layout.driver.VARIANTS", ("all",)):` (`from unittest.mock import patch`; `VARIANTS` is imported into the driver module at `driver.py:21` and iterated at `:314-320`) so only the `all` variant is processed and `versions["all"]` is the value asserted.

- [ ] **Step 2: Run to verify failure**

Run: `aq test tests/task_graph/test_layout_driver.py -k status_flip_in_all_variant -v`
Expected: FAIL on the last assertion (today `_seed_queue` dirties the parent `e`, and `_lay` reads its children).

- [ ] **Step 3: Implement the fast path in `_seed_queue`**

In `src/task_graph/layout/driver.py:478-483`, change the beginning of the loop from:

```python
        for tid, reason in self.marks:
            self.dirty_tasks.add(tid)
            if tid in self.snapshot:
                dirty.add(self.parent_of[tid])
```

to:

```python
        for tid, reason in self.marks:
            self.dirty_tasks.add(tid)
            if tid in self.snapshot:
                if await self._aggregates_only(tid, reason):
                    # Geometry cannot change: ``_refresh_aggregates`` walks
                    # ``dirty_tasks``' ancestors and rewrites the counters.
                    continue
                dirty.add(self.parent_of[tid])
```

and add the predicate as a method on `_IncrementalBatch` just above `_seed_queue`:

```python
    async def _aggregates_only(self, tid: str, reason: str) -> bool:
        """Can this mark be folded without re-laying any container?

        Only in the ``all`` variant, where every task is present whatever
        its status (``_visible``), and only for a status mark on a leaf that
        already has a stored row: its box is unchanged, so the only stale
        state is the ancestors' completed/active/running counters.  A
        container's own status can flip a stub in ``active``, and a task
        without a row still needs placing, so both fall through to the
        ordinary path.  Measured: the ordinary path re-flows the whole
        parent — 1.4 s for a 5,000-child root.
        """
        if self.variant != "all" or not reason.startswith("status."):
            return False
        task = self.snapshot[tid]
        if task.is_container:
            return False
        return await self._db_row(tid) is not None
```

`_db_row` for the dirty task is already primed by `_preload_db_rows`, so this adds no statement.

- [ ] **Step 4: Run the driver suite**

Run: `aq test tests/task_graph/test_layout_driver.py -v`
Expected: all PASS — in particular the existing incremental tests that reparent, delete, and reopen tasks still re-lay (their reasons are not `status.*`, or their variant is `active`).

- [ ] **Step 5: Benchmark**

Run: `BENCH_REUSE=1 .venv/bin/python <scratchpad>/bench.py 2>&1 | grep process_dirty`
Expected: the `all` half of each `process_dirty` no longer runs the engine; the `perf` cases drop noticeably (baseline 540–630 ms), the `flat` case roughly halves (baseline 1.4 s, `active` still re-flows). Record the numbers in the commit message.

- [ ] **Step 6: Commit**

```bash
git add src/task_graph/layout/driver.py tests/task_graph/test_layout_driver.py
git commit -m "perf(layout): status marks in the all variant refresh aggregates without re-laying"
```

### Task D3: Leaf removal without re-flow for `status.finished` in the `active` variant

**Files:**
- Modify: `src/task_graph/layout/driver.py` (`_seed_queue`, `_aggregates_only` from D2, and `run`)
- Test: `tests/task_graph/test_layout_driver.py`
- Modify: `docs/superpowers/specs/2026-09-01-task-graph-spatial-layout-design.md` §4.6 — one paragraph recording the behaviour

**Interfaces:**
- Produces: in variant `active`, a `status.finished` mark on a **non-container leaf that has a stored row** deletes that row (and its cells, via the existing `WriteSet.deletes` → `publish_layout` path) and refreshes ancestor aggregates, **without** re-laying the parent. The freed slot stays empty until the parent is next laid out for any other reason (a created/moved/reopened sibling, a tidy job, or the reconcile sweep). `status.reopened` and containers keep the ordinary path.

This is a visible behaviour change (a finished task leaves a gap instead of siblings closing up) and is the one item in this plan the operator should confirm before it ships. It is what turns "one task finishes in a 5,000-task flat project" from 1.4 s of engine time into a row delete.

- [ ] **Step 1: Write the failing test**

Append to `tests/task_graph/test_layout_driver.py`:

```python
async def test_finished_leaf_leaves_active_variant_without_relaying_siblings(db):
    kids = await seed_epic(db, n=4)
    drv = LayoutDriver(db)
    await drv.full_layout("p1", "all")
    await drv.full_layout("p1", "active")
    before = await db.load_layout_rows("p1", "active", ["e", *kids])

    await db.transition_task(kids[1], TaskStatus.COMPLETED, force=True)
    versions = await drv.process_dirty("p1", min_age_seconds=0)

    assert versions["active"] == 2
    after = await db.load_layout_rows("p1", "active", ["e", *kids])
    assert kids[1] not in after
    for k in (kids[0], kids[2], kids[3]):  # siblings did not close up
        assert (after[k].abs_x, after[k].abs_y) == (before[k].abs_x, before[k].abs_y)
    assert after["e"].agg_completed == 1 and after["e"].agg_active == 3
    assert (after["e"].w, after["e"].h) == (before["e"].w, before["e"].h)
```

- [ ] **Step 2: Run to verify failure**

Run: `aq test tests/task_graph/test_layout_driver.py -k finished_leaf_leaves_active -v`
Expected: FAIL — today the parent re-flows and `kids[2]` moves into the freed slot.

- [ ] **Step 3: Extend the fast path**

Replace the `_aggregates_only` method from D2 with:

```python
    async def _aggregates_only(self, tid: str, reason: str) -> bool:
        """Can this mark be folded without re-laying any container?

        ``all``: every task is present whatever its status, so a status mark
        on a leaf with a stored row changes no box — only the ancestors'
        counters.

        ``active``: a leaf that just FINISHED leaves the variant.  Its row is
        deleted here and the slot is left empty rather than re-flowing the
        parent (1.4 s for a 5,000-child root); the next ordinary pass over
        that container — a created, moved or reopened sibling, a tidy job,
        the reconcile sweep — closes the gap.  A REOPENED leaf needs a slot,
        and a container's status can turn it into a stub, so both take the
        ordinary path.
        """
        if not reason.startswith("status."):
            return False
        task = self.snapshot[tid]
        if task.is_container:
            return False
        row = await self._db_row(tid)
        if row is None:
            return False
        if self.variant == "all":
            return True
        if reason == "status.finished" and tid not in self.present:
            self.ws.deletes.append(tid)
            return True
        return False
```

In `run()` (`driver.py:~700`), the loop "Dirty tasks that vanished from this variant go, with their subtree" already calls `_delete_subtree(row.path)` for every dirty task not present; for the fast-path leaf that adds its own id a second time, which `sorted(set(self.ws.deletes) - set(self.pending))` de-duplicates. No change needed there — confirm by reading `driver.py:700-712` and note it in the PR.

- [ ] **Step 4: Run the driver suite**

Run: `aq test tests/task_graph/test_layout_driver.py tests/test_api_graph_layout.py -v`
Expected: PASS. `test_active_variant_excludes_finished_and_stubs_finished_epics` still passes because it goes through `full_layout`.

- [ ] **Step 5: Record the behaviour in the design spec**

In `docs/superpowers/specs/2026-09-01-task-graph-spatial-layout-design.md` §4.6 (incremental pass), add:

> **Status marks (2026-09-04).** A `status.*` mark on a leaf with a stored row is folded without re-laying its container: in `all` only the ancestors' aggregates are rewritten; in `active` a `status.finished` leaf's row is deleted and its slot left empty until the container is next laid out for any other reason. Re-flowing on every completion measured 1.4 s for a 5,000-child root and ran inside the orchestrator cycle.

- [ ] **Step 6: Benchmark and commit**

Run: `BENCH_REUSE=1 .venv/bin/python <scratchpad>/bench.py 2>&1 | grep process_dirty`
Expected: `process_dirty flat [f2500]` well under 100 ms (baseline 1.4 s). Note the reused database has already consumed earlier marks; the script re-marks each run.

```bash
git add src/task_graph/layout/driver.py tests/task_graph/test_layout_driver.py docs/superpowers/specs/2026-09-01-task-graph-spatial-layout-design.md
git commit -m "perf(layout): finished leaves leave the active variant without re-flowing siblings"
```

---

## Workstream E — websocket fan-out (finding §6)

### Task E1: Demote per-frame logs and serialize each event once

**Files:**
- Modify: `src/api/websocket.py:161-194` (`_on_event`), `:296-345` (send loop)
- Test: `tests/test_websocket_forwarding.py`

**Interfaces:**
- Queues now carry `tuple[dict, str]` — `(event, frame)` where `frame` is `json.dumps(event)` computed **once per event** for the common (non-question) case and once per client for question events (their payload is scope-filtered). The `seq` normalisation (`{**event, "seq": None}` when absent) moves into `_on_event` so the serialized frame is final.
- The replay branch keeps `send_json` (it already builds one frame per row).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_websocket_forwarding.py`:

```python
async def test_live_frames_are_serialized_once_per_event_and_logged_at_debug(caplog) -> None:
    import json
    import logging

    bus = EventBus(env="dev")
    manager = WebSocketManager(bus)
    q1: asyncio.Queue = asyncio.Queue()
    q2: asyncio.Queue = asyncio.Queue()
    manager._clients["c1"] = q1
    manager._clients["c2"] = q2
    manager._client_scope["c1"] = RequestScope(kind="local")
    manager._client_scope["c2"] = RequestScope(kind="local")
    manager.start()
    try:
        with caplog.at_level(logging.INFO, logger="src.api.websocket"):
            await bus.emit("task.updated", {"task_id": "t1", "project_id": "p1"})
    finally:
        manager.shutdown()

    e1, f1 = q1.get_nowait()
    e2, f2 = q2.get_nowait()
    assert f1 is f2  # the same serialized string object, not two dumps
    assert json.loads(f1)["seq"] is None and json.loads(f1)["task_id"] == "t1"
    assert e1 is e2
    assert not [r for r in caplog.records if r.levelno >= logging.INFO and "WS" in r.getMessage()]
```

`RequestScope(kind="local")` is exactly how `LOCAL_SCOPE` is built (`src/api/auth.py:67`); the `RequestScope` import already exists at the top of this test file.

- [ ] **Step 2: Run to verify failure**

Run: `aq test tests/test_websocket_forwarding.py -k serialized_once -v`
Expected: FAIL — today the queue holds a bare dict (`ValueError: too many values to unpack` or `TypeError`), and INFO records `WS forwarding event:` are present.

- [ ] **Step 3: Implement**

In `_on_event` (`websocket.py:161-194`):

```python
    def _on_event(self, data: dict[str, Any]) -> None:
        """Fan out allowed live events to all connected clients.

        The frame is serialized once per event, not once per client: at a
        busy fleet with several dashboard tabs open, per-client
        ``json.dumps`` plus three INFO log lines per frame was a steady
        CPU and log cost that scaled with clients × events.
        """
        event_type = data.get("_event_type", "")
        if not event_type.startswith(_FORWARDED_PREFIXES):
            return
        logger.debug("WS forwarding %s to %d clients", event_type, len(self._clients))

        # Live frames carry seq=None unless the emitter threaded the DB id
        # into the payload (log_event returns the id).
        shared = data if "seq" in data else {**data, "seq": None}
        shared_frame = json.dumps(shared)

        for ws, queue in list(self._clients.items()):
            event, frame = shared, shared_frame
            scope = self._client_scope.get(ws)
            if event_type.startswith("metrics.") and not _metrics_event_allowed(scope):
                continue
            if event_type.startswith("pool.") and not _pool_event_allowed(data, scope):
                continue
            if event_type in _QUESTION_EVENTS:
                filtered = _question_invalidation(data, scope)
                if filtered is None:
                    continue
                event = filtered if "seq" in filtered else {**filtered, "seq": None}
                frame = json.dumps(event)
            try:
                queue.put_nowait((event, frame))
            except asyncio.QueueFull:
                # Drop oldest event to make room
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait((event, frame))
                except asyncio.QueueFull:
                    pass
```

Update the queue type annotation in `handle` (`websocket.py:232`) to `asyncio.Queue[tuple[dict[str, Any], str]]`, and the class attribute `self._clients: dict[WebSocket, asyncio.Queue[tuple[dict[str, Any], str]]]`.

In the post-replay drain (`websocket.py:309-326`) the items are now tuples — change `seq = item.get("seq")` to `seq = item[0].get("seq")`.

Replace the live loop (`websocket.py:327-343`) with:

```python
            while True:
                event, frame = await queue.get()
                seq = event.get("seq")
                # Dedup: a live frame whose seq is <= the last replayed row id
                # would duplicate what the client already saw during replay.
                # Only applies when the emitter honestly threaded a persisted
                # row id; ``seq=None`` bypasses.
                if after_seq is not None and isinstance(seq, int) and seq <= last_replayed:
                    continue
                await websocket.send_text(frame)
```

Remove the two `logger.info("WS sending ..."/"WS sent successfully ...")` lines. Leave connection open/close logs at INFO.

- [ ] **Step 4: Run the websocket suites**

Run: `aq test tests/test_websocket_forwarding.py tests/test_agent_question_websocket.py tests/test_task_graph_live_events.py -v`
Expected: PASS. Any test that reads `queue.get_nowait()` as a dict must be updated to unpack `(event, frame)`; grep for `_clients[` in `tests/` to find them.

- [ ] **Step 5: Commit**

```bash
git add src/api/websocket.py tests/
git commit -m "perf(ws): serialize each live frame once and log fan-out at debug"
```

---

## Workstream F — dashboard (findings §5, §7)

### Task F1: Stop refetching the graph on `session.*` events

**Files:**
- Modify: `dashboard/src/pages/command-center/useGraphLive.ts:105-113`
- Test: `dashboard/src/pages/command-center/__tests__/useGraphLive.test.tsx`

**Interfaces:**
- `useGraphLive` keeps refetching on `task.*`, `gate.*`, `agent.*` and task-bearing `notify.*`. `session.*` frames carry no graph content (agents dock by `agent.current_task_id`, which arrives as `agent.updated`) and no longer schedule a refresh.

- [ ] **Step 1: Write the failing test**

Add to `useGraphLive.test.tsx` inside the existing `describe("shared task workspace live snapshots", ...)`. The harness there (`setup()` at line 38) returns `{ client, result, rerender, unmount }` from `renderHook`; `beforeEach` already opens the socket and seeds `snapshots`; frames are delivered with `socket().receive({...})` and carry `_event_type` (the wire field name), not `event_type`:

```tsx
  it("does not refetch the graph for session lifecycle frames", async () => {
    const { result } = setup();
    await waitFor(() => expect(result.current.data.tasks).toHaveLength(2));
    expect(transport.get).toHaveBeenCalledTimes(1);
    act(() => socket().receive({ _event_type: "session.started", project_id: "p1", session_id: "s1" }));
    act(() => socket().receive({ _event_type: "session.exited", project_id: "p1", session_id: "s1" }));
    // Past the 500 ms coalescing window: a scheduled refresh would have fired.
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 700)); });
    expect(transport.get).toHaveBeenCalledTimes(1);
  });
```

- [ ] **Step 2: Run to verify failure**

Run: `npm -w dashboard run test -- useGraphLive`
Expected: FAIL — `transport.get` called twice (a `session.*` frame currently schedules a refresh).

- [ ] **Step 3: Implement**

In `useGraphLive.ts:105-107` change:

```ts
    const graphEvent = /^(task|agent|gate|session)\./.test(type) || (
      type.startsWith("notify.") && !!task
    );
```

to:

```ts
    // session.* frames carry no graph content: an agent docking or leaving a
    // node arrives as agent.updated. Refetching the whole snapshot for every
    // session lifecycle frame multiplied the most expensive request the
    // dashboard makes (perf investigation 2026-09-04 §7).
    const graphEvent = /^(task|agent|gate)\./.test(type) || (
      type.startsWith("notify.") && !!task
    );
```

- [ ] **Step 4: Run tests, typecheck, lint**

Run: `npm -w dashboard run test -- useGraphLive && npm -w dashboard run typecheck && npm -w dashboard run lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/pages/command-center/useGraphLive.ts dashboard/src/pages/command-center/__tests__/useGraphLive.test.tsx
git commit -m "perf(dashboard): do not refetch the graph on session lifecycle frames"
```

### Task F2: Virtualize the Command Center task table

**Files:**
- Modify: `dashboard/package.json` (dependency)
- Modify: `dashboard/src/pages/command-center/Tasks.tsx`
- Test: `dashboard/src/pages/command-center/__tests__/Tasks.test.tsx`

**Interfaces:**
- Consumes: `@tanstack/react-virtual` `useVirtualizer` (row virtualizer over the scroll container).
- Produces: the table renders only the rows in the viewport plus an overscan of 12; `filtered` is memoised on `[tasks, projectId, filters, names]`. Row markup, `data-task-row`, `data-listnav`, and the click/keyboard behaviour are unchanged, so `Tasks.test.tsx` keeps passing (its three fixture rows all fit in the viewport). The `useListNav` keyboard walk only sees mounted rows, which is the visible window plus overscan — acceptable, and noted in the component.

- [ ] **Step 1: Install the dependency**

Run from the repo root: `npm install @tanstack/react-virtual@^3 -w dashboard`
Expected: `dashboard/package.json` gains `"@tanstack/react-virtual": "^3.x"` and the root `package-lock.json` updates.

- [ ] **Step 2: Write the failing test**

Append to `Tasks.test.tsx`'s `describe` block:

```tsx
  it("renders only the rows near the viewport when there are thousands of tasks", () => {
    const many = Array.from({ length: 3000 }, (_, i) => ({
      id: `bulk-${i}`, title: `Bulk ${i}`, project_id: "alpha", status: "READY", priority: 100,
    }));
    mocks.tasks.splice(0, mocks.tasks.length, ...many);
    render(<Tasks />);
    expect(screen.getByText("3000 tasks")).toBeInTheDocument();
    const rows = document.querySelectorAll("[data-task-row]");
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.length).toBeLessThan(200);
  });
```

Because `mocks.tasks` is reset by `beforeEach` only for `priority`, restore it at the end of the test (`mocks.tasks.splice(0, mocks.tasks.length, ...original)` where `original` is captured before the splice) or move the fixture reset into `beforeEach`.

jsdom reports zero heights, so `useVirtualizer` needs an explicit `estimateSize` and, in tests, an `initialRect`; the implementation below passes `initialRect: { width: 800, height: 600 }` so the virtualizer renders a bounded window under jsdom.

- [ ] **Step 3: Run to verify failure**

Run: `npm -w dashboard run test -- Tasks.test`
Expected: FAIL — 3,000 `[data-task-row]` elements rendered.

- [ ] **Step 4: Implement**

In `Tasks.tsx`:

1. Imports: add `useRef` to the React import and `import { useVirtualizer } from "@tanstack/react-virtual";`.
2. Memoise the filter and add the virtualizer after `names`:

```tsx
  const filtered = useMemo(
    () => tasks.filter((task) => (!projectId || task.project_id === projectId)
      && matchesTask(task, filters, names.get(task.project_id ?? "") ?? "")),
    [tasks, projectId, filters, names],
  );
  const scrollRef = useRef<HTMLDivElement>(null);
  // Only the rows in view are mounted: the graph snapshot carries every task
  // in the project, and a 5,000-row table with three interactive cells per
  // row re-rendered on every keystroke and every live refetch. Keyboard list
  // navigation (useListNav) walks mounted rows, i.e. the window + overscan.
  const virtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 64,
    overscan: 12,
    initialRect: { width: 800, height: 600 },
  });
  const items = virtualizer.getVirtualItems();
  const padTop = items.length ? items[0]!.start : 0;
  const padBottom = items.length ? virtualizer.getTotalSize() - items[items.length - 1]!.end : 0;
```

3. Put `ref={scrollRef}` on the outer `<div role="region" ...>` (it already has `overflow-auto`).
4. Replace `{filtered.map((task) => (` … `))}` with:

```tsx
          {padTop > 0 && <tr aria-hidden="true"><td colSpan={columns} style={{ height: padTop, padding: 0, border: 0 }} /></tr>}
          {items.map((item) => {
            const task = filtered[item.index]!;
            return (
              <tr key={task.id} data-index={item.index} ref={virtualizer.measureElement}
                tabIndex={0} data-listnav="1" data-task-row={task.id} aria-selected={selectedTaskId === task.id}
                onClick={(event) => {
                  if ((event.target as HTMLElement).closest('button, input, select, textarea, a, [role="dialog"]')) return;
                  selectTask(task);
                }}
                onKeyDown={(event) => {
                  if (event.target !== event.currentTarget) return;
                  if (event.key === "Enter" || event.key === " " || event.key === "o") {
                    event.preventDefault(); selectTask(task);
                  }
                }}
                className={`cursor-pointer focus:outline focus:outline-1 focus:outline-indigo-400 ${selectedTaskId === task.id ? "bg-indigo-500/15" : "hover:bg-gray-900/70"}`}>
                <td className="min-w-48 max-w-md px-3 py-3">
                  <span className="line-clamp-2 font-medium text-indigo-300">{task.title || task.id}</span>
                  <span className="mt-1 block font-mono text-[10px] text-gray-500">{task.id}</span>
                </td>
                {!projectId && <td className="max-w-40 truncate px-3 py-3 text-xs text-gray-400" title={task.project_id}>{names.get(task.project_id ?? "") || task.project_id}</td>}
                <td className="px-3 py-3"><InlineStatus task={task} /></td>
                <td className="px-3 py-3"><InlinePriority task={task} /></td>
                <td className="px-3 py-3 text-gray-400">{task.assigned_agent || "Unassigned"}</td>
                <td className="px-3 py-3"><RowActions task={task} /></td>
              </tr>
            );
          })}
          {padBottom > 0 && <tr aria-hidden="true"><td colSpan={columns} style={{ height: padBottom, padding: 0, border: 0 }} /></tr>}
```

The row's handlers, class names and five cells are the current ones from `Tasks.tsx:50-71` verbatim; only the wrapper changes (`data-index`, the `measureElement` ref, and reading `task` from `filtered[item.index]`). The existing `const filtered = tasks.filter(...)` line (`Tasks.tsx:25-26`) is replaced by the memoised version above.

- [ ] **Step 5: Run tests, typecheck, lint**

Run: `npm -w dashboard run test -- Tasks.test && npm -w dashboard run typecheck && npm -w dashboard run lint`
Expected: PASS, including the pre-existing tests in the file (3 fixture rows all render inside the 600 px initial rect).

- [ ] **Step 6: Commit**

```bash
git add package-lock.json dashboard/package.json dashboard/src/pages/command-center/Tasks.tsx dashboard/src/pages/command-center/__tests__/Tasks.test.tsx
git commit -m "perf(dashboard): virtualize the command center task table"
```

---

## Workstream G — `set_parent` cycle check (finding §4)

### Task G1: Targeted reachability instead of loading every blocking edge

**Files:**
- Modify: `src/database/queries/hierarchy_queries.py:316-327` (cycle check in `set_parent`) and `:530-543` (`_blocking_edges`)
- Test: `tests/test_hierarchy_queries.py`

**Interfaces:**
- Produces: `HierarchyQueryMixin._reaches_over_blocking_edges(conn, start: str, target: str) -> bool` — `True` iff `target` is reachable from `start` by following `task_dependencies` rows of a type in `BLOCKING_DEP_TYPES` in the `task_id -> depends_on_task_id` direction. One recursive-CTE statement bounded to the reachable set, not the table. Adding edge `task_id -> parent_id` closes a cycle iff `parent_id` already reaches `task_id`.
- `_blocking_edges` is removed once nothing else calls it (`grep -rn "_blocking_edges" src tests`).

The existing test `test_set_parent_rejects_blocking_dependency_cycle_on_both_backends` (`tests/test_hierarchy_queries.py:132`) pins the behaviour on SQLite and PostgreSQL.

- [ ] **Step 1: Write the failing statement-shape test**

Append to `tests/test_hierarchy_queries.py`. Its `db` fixture (line 36) creates one project whose id is the module constant `PROJECT_ID`; `Task`, `HierarchyError` and `select` are already imported:

```python
from sqlalchemy import event


async def test_set_parent_cycle_check_does_not_load_the_whole_edge_table(db):
    # 200 unrelated blocking edges elsewhere in the project.
    for i in range(200):
        await db.create_task(Task(id=f"u{i}", project_id=PROJECT_ID, title="", description=""))
        if i:
            await db.add_dependency(f"u{i}", f"u{i-1}")
    for tid in ("parent", "child"):
        await db.create_task(Task(id=tid, project_id=PROJECT_ID, title=tid, description=""))

    statements: list[str] = []

    def _hook(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db._engine.sync_engine, "before_cursor_execute", _hook)
    try:
        async with db._engine.begin() as conn:
            await db.set_parent("child", "parent", conn=conn)
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", _hook)

    unfiltered = [
        s for s in statements
        if "task_dependencies" in s and "WHERE" in s
        and "task_id" not in s.split("WHERE", 1)[1] and "depends_on_task_id" not in s.split("WHERE", 1)[1]
    ]
    assert unfiltered == [], unfiltered  # every edge read is anchored on an id
```

`_blocking_edges` today emits `SELECT task_id, depends_on_task_id FROM task_dependencies WHERE dep_type IN (...)` — no id in the WHERE clause — which is exactly what the assertion catches.

- [ ] **Step 2: Run to verify failure**

Run: `aq test tests/test_hierarchy_queries.py -k whole_edge_table -v`
Expected: FAIL with one unfiltered statement listed.

- [ ] **Step 3: Implement the reachability CTE**

In `hierarchy_queries.py`, replace `_blocking_edges` (`:530-543`) with:

```python
    async def _reaches_over_blocking_edges(self, conn, start: str, target: str) -> bool:
        """Is *target* reachable from *start* along blocking edges?

        Follows ``task_id -> depends_on_task_id`` (the "X blocks-on Y"
        direction) over ``BLOCKING_DEP_TYPES`` only, as one recursive CTE
        bounded by the reachable set.  Replaces loading every blocking edge
        in the database and running ``validate_dag_with_new_edge`` in
        Python, which cost ~6 ms of SQL plus the graph build per call and
        grew with the whole database rather than the subtree (93 ms per
        ``set_parent`` at 14k edges).
        """
        from sqlalchemy import literal

        from src.models import BLOCKING_DEP_TYPES

        blocking = sorted(BLOCKING_DEP_TYPES)
        base = (
            select(task_dependencies.c.depends_on_task_id.label("id"), literal(1).label("depth"))
            .where(
                task_dependencies.c.task_id == start,
                task_dependencies.c.dep_type.in_(blocking),
            )
            .cte("reach", recursive=True)
        )
        step = (
            select(task_dependencies.c.depends_on_task_id, (base.c.depth + 1).label("depth"))
            .where(
                task_dependencies.c.task_id == base.c.id,
                task_dependencies.c.dep_type.in_(blocking),
                base.c.depth < MAX_STRUCTURAL_DEPTH * 64,  # loop guard on a drifted graph
            )
        )
        reach = base.union(step)  # UNION (not ALL) so a pre-existing cycle terminates
        row = (
            await conn.execute(select(reach.c.id).where(reach.c.id == target).limit(1))
        ).fetchone()
        return row is not None
```

`select` and `task_dependencies` are already imported in that module; `MAX_STRUCTURAL_DEPTH` is already used at `:330`.

Replace the check in `set_parent` (`:316-327`):

```python
            deps = await self._blocking_edges(conn)
            try:
                validate_dag_with_new_edge(deps, task_id, parent_id, DepType.PARENT_CHILD.value)
            except CyclicDependencyError as exc:
                raise HierarchyError("cycle", str(exc)) from exc
```

with:

```python
            # The new edge is task_id -> parent_id.  It closes a cycle iff
            # parent_id already reaches task_id over blocking edges.
            if await self._reaches_over_blocking_edges(conn, parent_id, task_id):
                raise HierarchyError(
                    "cycle", f"{parent_id} already depends on {task_id} through blocking edges"
                )
```

Remove the now-unused imports (`validate_dag_with_new_edge`, `CyclicDependencyError`) if `ruff check src/database/queries/hierarchy_queries.py` reports them unused.

- [ ] **Step 4: Run the hierarchy suites on both backends**

Run: `POSTGRES_TEST_DSN=postgresql://agent_queue:agent_queue_dev@localhost:5533/aq_test_hier aq test tests/test_hierarchy_queries.py tests/test_hierarchy_commands.py tests/test_hierarchy_graph_creator.py -v`
Expected: PASS, including `test_set_parent_rejects_blocking_dependency_cycle_on_both_backends` (which now hits the CTE path). `tests/pg_dsn.py` creates the named database if it does not exist (memory: use a throwaway base database, never the shared master).

- [ ] **Step 5: Benchmark and commit**

Run: `BENCH_REUSE=1 .venv/bin/python <scratchpad>/bench2.py 2>&1 | grep set_parent`
Expected: `set_parent` under 40 ms (baseline 92–95 ms, 26 statements; the recompute and the depth CTEs remain).

```bash
git add src/database/queries/hierarchy_queries.py tests/test_hierarchy_queries.py
git commit -m "perf(hierarchy): targeted reachability check in set_parent instead of loading every edge"
```

---

## Closing task

### Task H1: Area-wide runs, regenerate nothing, update docs

- [ ] **Step 1: One broader run per touched area**

```bash
aq test tests/test_api_graph.py tests/test_dependency_queries.py tests/test_work_graph_cascade.py tests/test_orchestrator.py tests/test_hierarchy*.py tests/task_graph/ tests/test_websocket_forwarding.py tests/test_agent_question_websocket.py tests/test_task_graph_live_events.py tests/test_database.py
npm -w dashboard run test && npm -w dashboard run typecheck && npm -w dashboard run lint
ruff check src/api/graph.py src/api/websocket.py src/orchestrator/monitoring.py src/orchestrator/layout_step.py src/orchestrator/core.py src/database/ src/task_graph/layout/driver.py
```

No `src/api/models` or codegen router changed, so `openapi.json` and the generated clients need no regeneration; confirm with `aq test tests/test_api_client_contract.py -k committed_openapi`.

- [ ] **Step 2: Record the after-numbers**

Append an "After" table to `docs/superpowers/specs/2026-09-04-dashboard-performance-investigation.md` with the same rows as its "Headline numbers" table, measured with the scratchpad scripts against `aq_perfprobe` after all tasks.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-09-04-dashboard-performance-investigation.md
git commit -m "docs(perf): record post-change measurements"
```

## Deferred (deliberately not in this plan)

- Server-side paged/sorted task list endpoint: after A3 the snapshot is ~60 ms and F2 makes rendering O(viewport); paging becomes worth it only past ~20k tasks.
- `pg_trgm` index for title search: needs `CREATE EXTENSION`, an operator decision per deployment.
- Graph snapshot versioning / 304s: the batched endpoint is cheap enough that the 500 ms coalesced refetch is fine; revisit if `GET /graph` shows up in `pg_stat_statements` after C2.
- Narrow projections for the monitoring cycle's `list_tasks(status=...)` calls (75 ms → 9 ms each): worthwhile, but the `Task` object is threaded through `transition_task` contexts; do it as its own change with its own tests.
- Websocket micro-batching: no evidence yet that frame *count* is the bottleneck once E1 lands.
