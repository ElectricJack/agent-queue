# Dashboard v2 Phase 4: Command Center Graph — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the live pan/zoom Command Center: a per-project graph API endpoint plus a React Flow canvas with live WS updates, agent avatars, task sidebar, and a ghost-overlay slot for later proposal previews.

**Architecture:** One aggregate REST endpoint per project (`GET /api/projects/{project_id}/graph`) assembles tasks + typed edges + gates + agents from existing query mixins, returning a snapshot the client lays out. The dashboard fetches one snapshot per selected project via TanStack Query, merges them client-side, renders with **@xyflow/react v12** using **dagre** for auto-layout, and mutates the react-query cache incrementally on WS events (no per-event refetch) with a 60 s periodic reconciliation refetch.

**Tech Stack:** FastAPI + SQLAlchemy Core (backend); Vite + React 19 + TanStack Query v5 + @xyflow/react v12 + @dagrejs/dagre + Tailwind v4 (frontend). Tests: pytest-asyncio for backend (mirrors `tests/test_api_messages.py`, `tests/test_session_stream_api.py`); `npm run typecheck` + `npm run lint` for frontend (no unit-test tooling exists in `dashboard/` today — verified in `dashboard/package.json`).

## Global Constraints

- Python 3.12+, ruff line-length 100, py312 target.
- Backend must not call any LLM in the control path (spec §1).
- All frontend daemon I/O goes through `@aq/ts-client`; **never** call `fetch` directly (dashboard/CLAUDE.md). If the endpoint isn't generated yet, add it to `src/api/models/task.py` + a router, then regenerate: `npm run generate:ts-client` from repo root with daemon running.
- Auto-generated command routes come from `src/api/codegen.build_category_routers()`; this endpoint is *not* a command — mount it via an explicit `APIRouter` (pattern: `src/api/sessions.py`).
- No new task-lifecycle statuses (spec §3). `intelligence_class`, `routing` gate type, `GET /api/proposals/{id}` are owned by other phases — reference only.
- React Query keys: `["projectGraph", projectId]`. Invalidation must be **surgical** — see live-update task; do not blanket-invalidate on every task event.
- Cross-project edges render only when both endpoint tasks are in a currently-loaded project (§9.2).
- Mobile: `< 768 px` degrades to project strip + status-grouped card list; canvas is available in landscape only.
- Icons from `@heroicons/react/24/outline` only.

---

## File Structure

**Backend (create):**
- `src/api/graph.py` — router factory + default router (mirrors `src/api/sessions.py` layout).
- `src/api/models/graph.py` — Pydantic response models.
- `tests/test_api_graph.py` — pytest-asyncio tests using `AsyncClient` + `ASGITransport` (pattern: `tests/test_session_stream_api.py`).

**Backend (modify):**
- `src/api/app.py` — include the new router before `register_all_routers` so its concrete path wins.
- `src/api/models/__init__.py` — export new models (if `__init__.py` re-exports; verify first).

**Frontend (create):**
- `dashboard/src/pages/CommandCenter.tsx` — page component + route wiring shell.
- `dashboard/src/pages/command-center/ProjectStrip.tsx` — per-project vitals cards + multi-select.
- `dashboard/src/pages/command-center/GraphCanvas.tsx` — React Flow canvas + dagre layout hook.
- `dashboard/src/pages/command-center/TaskNode.tsx` — custom node renderer.
- `dashboard/src/pages/command-center/AgentAvatarLayer.tsx` — overlay of animated agent avatars.
- `dashboard/src/pages/command-center/TaskSidebar.tsx` — desktop sidebar / mobile bottom sheet.
- `dashboard/src/pages/command-center/GhostOverlay.tsx` — proposal preview overlay (feature-detects Phase 6).
- `dashboard/src/pages/command-center/useGraphLive.ts` — WS-event → react-query cache patcher.
- `dashboard/src/pages/command-center/layout.ts` — dagre wrapper.
- `dashboard/src/pages/command-center/types.ts` — client-side merged graph types.
- `dashboard/src/api/graph.ts` — thin hook wrapping the generated SDK call for `getProjectGraph` + merge helper.

**Frontend (modify):**
- `dashboard/src/App.tsx` — replace the Phase 3 Command Center placeholder route (add `<Route path="command-center" element={<CommandCenter />} />`).
- `dashboard/src/components/Sidebar.tsx` — add "Command Center" nav link if not already added by Phase 3.
- `dashboard/package.json` — add `@xyflow/react ^12` and `@dagrejs/dagre ^1`.
- `dashboard/src/ws/useEventStream.ts` — extend to invalidate `["projectGraph"]` on the event types the graph consumes (see Task 8).

---

## Design Decisions (locked)

- **Layout library: dagre.** Justification: pure-JS, ~15 KB gz, synchronous, deterministic. Elkjs is ~500 KB and async (WebWorker). Our DAGs are small (~10²–10³ nodes per project); dagre's speed suffices and the sync API lets us relayout inside a react-query cache-update callback without race conditions.
- **Graph library: @xyflow/react v12 (React Flow).** MIT-licensed, first-class React 19 support, built-in pan/zoom, custom node types, minimap, edge markers. No competing option delivers this out of the box.
- **Live-update strategy: incremental cache patching.** WS events (`task.blocked`, `task.unblocked`, `gate.created`, `gate.resolved`, `session.started`, `session.exited`, `notify.task_started`, `notify.task_completed`) mutate the cached graph payload via `queryClient.setQueryData(["projectGraph", pid], patcher)`. A React Flow node key stays stable across patches so React Flow diff-renders (no layout thrash for status-only changes). A **60 s** background refetch reconciles drift.
- **Agent motion:** avatars render in a separate `AgentAvatarLayer` positioned in Command Center screen space, sourcing coordinates from React Flow's `getNode(taskId).position` projected via `useReactFlow().flowToScreenPosition`. Assignment change ⇒ new target coord ⇒ CSS `transform: translate(...)` with `transition: transform 600ms ease`.
- **Ghost overlay:** `GhostOverlay` fetches `GET /api/proposals/{id}` (Phase 6). On 404 it renders `null`; a `console.debug` note explains the no-op. Zero visual noise until Phase 6 lands the endpoint.

---

## Task 1: Backend — Pydantic models for the graph payload

**Files:**
- Create: `src/api/models/graph.py`
- Modify: `src/api/models/__init__.py` (only if that file currently re-exports peer models; otherwise skip)
- Test: covered by later API tests

**Interfaces:**
- Consumes: `src.models.Task`, `src.models.DepType`
- Produces:
  ```python
  class GraphTaskNode(BaseModel):
      id: str
      title: str
      status: str
      priority: int
      is_blocked: bool
      profile_id: str | None
      intelligence_class: str | None  # Phase 1 field; may be absent on older rows
      assigned_agent_id: str | None
      branch_name: str | None
      pr_url: str | None

  class GraphEdge(BaseModel):
      from_task_id: str    # serialised as "from" via Field(alias="from")
      to_task_id: str      # serialised as "to" via Field(alias="to")
      dep_type: str

  class GraphGate(BaseModel):
      id: str
      gate_type: str
      status: str
      task_ids: list[str]

  class GraphAgent(BaseModel):
      id: str
      name: str
      profile_id: str | None
      current_task_id: str | None
      session_id: str | None

  class ProjectGraphResponse(BaseModel):
      tasks: list[GraphTaskNode]
      edges: list[GraphEdge]
      gates: list[GraphGate]
      agents: list[GraphAgent]
  ```

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api_graph.py` (create the file with this content):

```python
"""GET /api/projects/{project_id}/graph — aggregate graph payload."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.graph import build_graph_router
from src.database import Database
from src.models import Agent, AgentState, Project, Task


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "g.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="P1"))
    yield database
    await database.close()


@pytest.fixture
def client_factory(db):
    def _make() -> AsyncClient:
        app = FastAPI()
        app.include_router(build_graph_router(db=db))
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
    return _make


async def test_empty_project_returns_empty_arrays(client_factory):
    async with client_factory() as ac:
        r = await ac.get("/api/projects/p1/graph")
    assert r.status_code == 200
    body = r.json()
    assert body == {"tasks": [], "edges": [], "gates": [], "agents": []}


async def test_unknown_project_is_404(client_factory):
    async with client_factory() as ac:
        r = await ac.get("/api/projects/nope/graph")
    assert r.status_code == 404


async def test_tasks_edges_gates_agents_are_included(db, client_factory):
    await db.create_task(Task(id="t1", project_id="p1", title="One"))
    await db.create_task(Task(id="t2", project_id="p1", title="Two"))
    await db.add_dependency("t2", "t1")  # t2 blocks-on t1

    await db.create_agent(Agent(
        id="a1", name="claude-1", profile_id="claude-agent",
        state=AgentState.IDLE, current_task_id="t1",
    ))
    # Attach a gate via the gate-command surface (or db helper) — see plan §gate helper.
    gid = await db.create_gate(
        project_id="p1", gate_type="human", title="review",
        created_by="test", data={},
    )
    await db.attach_gate_to_task(gid, "t1")

    async with client_factory() as ac:
        r = await ac.get("/api/projects/p1/graph")
    assert r.status_code == 200
    body = r.json()

    ids = {t["id"] for t in body["tasks"]}
    assert ids == {"t1", "t2"}
    assert {(e["from"], e["to"], e["dep_type"]) for e in body["edges"]} \
        == {("t2", "t1", "blocks")}
    assert body["gates"][0]["task_ids"] == ["t1"]
    assert body["agents"][0]["current_task_id"] == "t1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_graph.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_graph_router' from 'src.api.graph'`.

- [ ] **Step 3: Create the response models**

Create `src/api/models/graph.py`:

```python
"""Response models for the aggregate project-graph endpoint (Phase 4)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GraphTaskNode(BaseModel):
    id: str
    title: str
    status: str
    priority: int = 100
    is_blocked: bool = False
    profile_id: str | None = None
    intelligence_class: str | None = None
    assigned_agent_id: str | None = None
    branch_name: str | None = None
    pr_url: str | None = None


class GraphEdge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_task_id: str = Field(alias="from")
    to_task_id: str = Field(alias="to")
    dep_type: str


class GraphGate(BaseModel):
    id: str
    gate_type: str
    status: str
    task_ids: list[str] = []


class GraphAgent(BaseModel):
    id: str
    name: str
    profile_id: str | None = None
    current_task_id: str | None = None
    session_id: str | None = None


class ProjectGraphResponse(BaseModel):
    tasks: list[GraphTaskNode] = []
    edges: list[GraphEdge] = []
    gates: list[GraphGate] = []
    agents: list[GraphAgent] = []
```

- [ ] **Step 4: Run test — still failing (endpoint missing)**

Run: `pytest tests/test_api_graph.py -v`
Expected: FAIL — same ImportError, now on `build_graph_router`.

- [ ] **Step 5: Commit**

```bash
git add src/api/models/graph.py tests/test_api_graph.py
git commit -m "feat(api): add ProjectGraphResponse models for command-center graph endpoint"
```

---

## Task 2: Backend — Router factory that assembles the payload

**Files:**
- Create: `src/api/graph.py`
- Modify: `src/api/app.py` (mount the router)
- Test: `tests/test_api_graph.py` (already written)

**Interfaces:**
- Consumes: `db.get_project`, `db.list_tasks(project_id=…)`, `db.get_all_dependencies(dep_types=None)` (filter to this project's task ids client-side within the router), `db.list_gates(project_id=…)`, `db.get_gate_waiters(gate_id)`, `db.list_agents()` filtered to `current_task_id ∈ project_task_ids`.
- Produces: `GET /api/projects/{project_id}/graph` → `ProjectGraphResponse`.

- [ ] **Step 1: Verify `Task` model exposes `intelligence_class`**

Run: `grep -n "intelligence_class" src/models.py`
If missing: **stop** — Phase 1 owns that column. Emit the field as `None` for now and note it in the response docstring; frontend already handles nullable.

- [ ] **Step 2: Write the router**

Create `src/api/graph.py`:

```python
"""Aggregate project-graph endpoint (Phase 4 — Command Center canvas).

Mirrors the router-factory pattern of :mod:`src.api.sessions` so tests can
wire a lightweight ``db`` without booting the full daemon.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.api.models.graph import (
    GraphAgent,
    GraphEdge,
    GraphGate,
    GraphTaskNode,
    ProjectGraphResponse,
)

__all__ = ["build_graph_router", "router"]


def build_graph_router(*, db) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/api/projects/{project_id}/graph",
        response_model=ProjectGraphResponse,
    )
    async def get_project_graph(project_id: str) -> ProjectGraphResponse:
        project = await db.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"No project '{project_id}'")

        tasks = await db.list_tasks(project_id=project_id)
        task_ids = {t.id for t in tasks}

        # Edges — pull every typed edge, then keep the ones whose "from" task
        # lives in this project.  Cross-project edges land on the client side
        # only when the peer project's graph is also loaded (spec §9.2).
        all_edges = await db.get_all_dependencies(dep_types=None)
        edges = [
            GraphEdge(
                from_task_id=e[0],  # (task_id, depends_on_task_id, dep_type)
                to_task_id=e[1],
                dep_type=e[2],
            )
            for e in all_edges
            if e[0] in task_ids
        ]

        gate_rows = await db.list_gates(project_id=project_id)
        gates: list[GraphGate] = []
        for g in gate_rows:
            waiters = await db.get_gate_waiters(g["id"])
            gates.append(GraphGate(
                id=g["id"],
                gate_type=g["gate_type"],
                status=g["status"],
                task_ids=sorted(waiters),
            ))

        # Agents currently assigned to a task in this project.
        all_agents = await db.list_agents()
        agents = [
            GraphAgent(
                id=a.id,
                name=a.name,
                profile_id=a.profile_id,
                current_task_id=a.current_task_id,
                session_id=getattr(a, "session_id", None),
            )
            for a in all_agents
            if a.current_task_id in task_ids
        ]

        return ProjectGraphResponse(
            tasks=[
                GraphTaskNode(
                    id=t.id,
                    title=t.title,
                    status=t.status.value if hasattr(t.status, "value") else str(t.status),
                    priority=t.priority,
                    is_blocked=t.is_blocked,
                    profile_id=t.profile_id,
                    intelligence_class=getattr(t, "intelligence_class", None),
                    assigned_agent_id=t.assigned_agent_id,
                    branch_name=t.branch_name,
                    pr_url=t.pr_url,
                )
                for t in tasks
            ],
            edges=edges,
            gates=gates,
            agents=agents,
        )

    return router


def _build_default_router() -> APIRouter:
    """Registered in :func:`src.api.app.create_app` — uses the shared db."""
    from src.api import dependencies as deps
    from fastapi import Request

    router = APIRouter()

    @router.get("/api/projects/{project_id}/graph")
    async def get_project_graph(project_id: str, request: Request):
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        inner = build_graph_router(db=orch.db)
        for route in inner.routes:
            if getattr(route, "path", None) == "/api/projects/{project_id}/graph":
                return await route.endpoint(project_id=project_id)
        raise HTTPException(status_code=500, detail="graph router misconfigured")

    return router


router = _build_default_router()
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `pytest tests/test_api_graph.py -v`
Expected: PASS on all three tests. If `db.create_gate` / `db.attach_gate_to_task` don't exist under those names, run `grep -n "def create_gate\|def attach_gate_to_task" src/database/queries/gate_queries.py` and swap in the actual helper names; update the test accordingly.

- [ ] **Step 4: Mount router in `create_app`**

Edit `src/api/app.py`. After the `messages_router` include and before `register_all_routers`, add:

```python
from src.api.graph import router as graph_router
app.include_router(graph_router)
```

- [ ] **Step 5: Add response-model registration for SDK codegen**

Add `src/api/models/task.py` **is not** the right file — this endpoint is not a `CommandHandler` command. Instead, ensure the FastAPI `response_model=ProjectGraphResponse` is set on the route (already done in Step 2). Verify the OpenAPI schema surfaces it: start the daemon and hit `curl -s http://127.0.0.1:8081/openapi.json | python -c "import json,sys; s=json.load(sys.stdin); assert '/api/projects/{project_id}/graph' in s['paths']"`.

- [ ] **Step 6: Commit**

```bash
git add src/api/graph.py src/api/app.py
git commit -m "feat(api): serve /api/projects/{id}/graph for command-center canvas"
```

---

## Task 3: Regenerate the TS client

**Files:**
- Modify: `packages/aq-client/**` (auto-generated) and `openapi.json` (cached spec).

**Interfaces:**
- Produces: `getProjectGraph({ path: { project_id: string } })` in `@aq/ts-client`.

- [ ] **Step 1: Start the daemon**

Run: `./run.sh start`
Verify: `curl -s http://127.0.0.1:8081/health | jq .status` returns `"ok"`.

- [ ] **Step 2: Regenerate the client**

Run (from repo root): `npm run generate:ts-client`
Expected: no errors; `packages/aq-client/**` files change.

- [ ] **Step 3: Verify the new operation is present**

Run: `grep -n "getProjectGraph\|project_id.*graph" packages/aq-client/src/index.ts packages/aq-client/src/**/*.ts | head`
Expected: at least one match for `getProjectGraph`.

- [ ] **Step 4: Typecheck the dashboard**

Run: `cd dashboard && npm run typecheck`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add packages/aq-client openapi.json
git commit -m "chore(ts-client): regenerate for project-graph endpoint"
```

---

## Task 4: Frontend — add React Flow + dagre dependencies

**Files:**
- Modify: `dashboard/package.json`

- [ ] **Step 1: Install deps**

Run: `cd dashboard && npm install @xyflow/react@^12 @dagrejs/dagre@^1`
Expected: `package.json` + `package-lock.json` updated.

- [ ] **Step 2: Confirm they resolve**

Run: `cd dashboard && npm run typecheck`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add dashboard/package.json dashboard/package-lock.json
git commit -m "feat(dashboard): add @xyflow/react + dagre for command-center graph"
```

---

## Task 5: Frontend — dagre layout helper

**Files:**
- Create: `dashboard/src/pages/command-center/layout.ts`
- Create: `dashboard/src/pages/command-center/types.ts`

**Interfaces:**
- Consumes: `ProjectGraphResponse` from `@aq/ts-client`.
- Produces:
  ```ts
  export interface MergedGraph {
    tasks: GraphTaskNode[];
    edges: GraphEdge[];
    gates: GraphGate[];
    agents: GraphAgent[];
  }
  export function layoutGraph(g: MergedGraph): { nodes: Node[]; edges: Edge[] };
  ```

- [ ] **Step 1: Write types.ts**

Create `dashboard/src/pages/command-center/types.ts`:

```ts
import type { ProjectGraphResponse } from "@aq/ts-client";

export type GraphTaskNode = ProjectGraphResponse["tasks"][number];
export type GraphEdge = ProjectGraphResponse["edges"][number];
export type GraphGate = ProjectGraphResponse["gates"][number];
export type GraphAgent = ProjectGraphResponse["agents"][number];

export interface MergedGraph {
  tasks: GraphTaskNode[];
  edges: GraphEdge[];
  gates: GraphGate[];
  agents: GraphAgent[];
  /** projectId a task belongs to — filled by the merger, keyed by task.id */
  taskProject: Record<string, string>;
}
```

- [ ] **Step 2: Write layout.ts**

Create `dashboard/src/pages/command-center/layout.ts`:

```ts
import dagre from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/react";
import type { MergedGraph } from "./types";

const NODE_WIDTH = 220;
const NODE_HEIGHT = 88;

export function layoutGraph(g: MergedGraph): { nodes: Node[]; edges: Edge[] } {
  const dg = new dagre.graphlib.Graph();
  dg.setDefaultEdgeLabel(() => ({}));
  dg.setGraph({ rankdir: "LR", nodesep: 40, ranksep: 80 });

  for (const t of g.tasks) {
    dg.setNode(t.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  // Only render edges whose BOTH endpoints are loaded (spec §9.2 cross-project rule).
  const loaded = new Set(g.tasks.map((t) => t.id));
  const edges = g.edges.filter((e) => loaded.has(e.from) && loaded.has(e.to));
  for (const e of edges) {
    dg.setEdge(e.from, e.to);
  }

  dagre.layout(dg);

  const nodes: Node[] = g.tasks.map((t) => {
    const pos = dg.node(t.id);
    // Attach gates that reference this task so the node renderer can badge them.
    const nodeGates = g.gates.filter((gate) => gate.task_ids.includes(t.id));
    return {
      id: t.id,
      type: "task",
      position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 },
      data: { task: t, gates: nodeGates, projectId: g.taskProject[t.id] },
    };
  });

  const rfEdges: Edge[] = edges.map((e) => ({
    id: `${e.from}->${e.to}:${e.dep_type}`,
    source: e.from,
    target: e.to,
    type: e.dep_type === "blocks" ? "smoothstep" : "default",
    animated: e.dep_type === "waits_for",
    style: edgeStyleForType(e.dep_type),
  }));

  return { nodes, edges: rfEdges };
}

function edgeStyleForType(depType: string): React.CSSProperties {
  switch (depType) {
    case "blocks":            return { stroke: "#818cf8", strokeWidth: 2 };
    case "parent_child":      return { stroke: "#a3a3a3", strokeDasharray: "4 4" };
    case "waits_for":         return { stroke: "#fbbf24", strokeWidth: 2 };
    case "conditional_blocks":return { stroke: "#fb923c", strokeDasharray: "6 3" };
    case "discovered_from":   return { stroke: "#6b7280", strokeDasharray: "2 4" };
    default:                  return { stroke: "#4b5563" };
  }
}
```

- [ ] **Step 3: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/pages/command-center/layout.ts dashboard/src/pages/command-center/types.ts
git commit -m "feat(dashboard): dagre layout helper for command-center canvas"
```

---

## Task 6: Frontend — TaskNode renderer

**Files:**
- Create: `dashboard/src/pages/command-center/TaskNode.tsx`

**Interfaces:**
- Consumes: `{ task: GraphTaskNode, gates: GraphGate[], projectId: string }` from `Node.data`.
- Produces: default-exported React Flow custom node registered as `type: "task"`.

- [ ] **Step 1: Write TaskNode.tsx**

Create `dashboard/src/pages/command-center/TaskNode.tsx`:

```tsx
import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { GraphGate, GraphTaskNode } from "./types";

interface TaskNodeData {
  task: GraphTaskNode;
  gates: GraphGate[];
  projectId: string;
}

const STATUS_TONE: Record<string, string> = {
  DEFINED: "border-gray-600 bg-gray-900 text-gray-200",
  READY: "border-sky-500 bg-sky-950 text-sky-100",
  IN_PROGRESS: "border-indigo-500 bg-indigo-950 text-indigo-100",
  COMPLETED: "border-emerald-500 bg-emerald-950 text-emerald-100",
  FAILED: "border-red-500 bg-red-950 text-red-100",
  BLOCKED: "border-amber-500 bg-amber-950 text-amber-100",
};

function priorityBorderClass(p: number): string {
  if (p <= 20) return "ring-2 ring-red-400";
  if (p <= 50) return "ring-2 ring-amber-400";
  if (p <= 100) return "ring-1 ring-gray-500";
  return "";
}

function gateBadge(gate: GraphGate) {
  const label =
    gate.gate_type === "routing" ? "⏳"
    : gate.gate_type === "review" || gate.gate_type === "task" ? "\u{1F50D}"
    : gate.gate_type === "pr-merged" ? "\u{1F500}"
    : "❗";
  return (
    <span key={gate.id} title={`${gate.gate_type} — ${gate.status}`}
          className="ml-1 text-xs">{label}</span>
  );
}

export default function TaskNode({ data, selected }: NodeProps<TaskNodeData>) {
  const { task, gates } = data;
  const tone = STATUS_TONE[task.status] ?? STATUS_TONE.DEFINED;
  const spinner = task.status === "IN_PROGRESS"
    ? <span className="absolute -top-1 -right-1 h-3 w-3 animate-spin rounded-full border-2 border-indigo-400 border-t-transparent" />
    : null;
  const completedFlash = task.status === "COMPLETED"
    ? <span className="absolute inset-0 -z-10 animate-pulse rounded bg-emerald-500/10" />
    : null;

  return (
    <div className={`relative rounded border p-2 text-xs shadow ${tone} ${priorityBorderClass(task.priority)} ${selected ? "outline outline-2 outline-white" : ""}`}
         style={{ width: 220, minHeight: 88 }}>
      {completedFlash}
      {spinner}
      <Handle type="target" position={Position.Left} />
      <div className="mb-1 flex items-center justify-between">
        <span className="font-mono text-[10px] opacity-70">{task.id.slice(0, 8)}</span>
        <span className="uppercase tracking-wide text-[9px]">{task.status}</span>
      </div>
      <div className="line-clamp-2 font-medium">{task.title}</div>
      <div className="mt-1 flex items-center gap-1 text-[10px] opacity-80">
        {task.profile_id && <span className="rounded bg-white/5 px-1">{task.profile_id}</span>}
        {task.intelligence_class && <span className="rounded bg-white/5 px-1">{task.intelligence_class}</span>}
        {gates.map(gateBadge)}
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
```

- [ ] **Step 2: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/pages/command-center/TaskNode.tsx
git commit -m "feat(dashboard): TaskNode renderer with status/priority/gate badges"
```

---

## Task 7: Frontend — graph fetch hook + client-side merger

**Files:**
- Create: `dashboard/src/api/graph.ts`

**Interfaces:**
- Consumes: `getProjectGraph` from `@aq/ts-client`, `useQueries` from `@tanstack/react-query`.
- Produces:
  ```ts
  export function useProjectGraphs(projectIds: string[]): {
    data: MergedGraph;
    isLoading: boolean;
    errors: (Error | null)[];
  };
  export const projectGraphKey = (pid: string) => ["projectGraph", pid] as const;
  ```

- [ ] **Step 1: Write api/graph.ts**

Create `dashboard/src/api/graph.ts`:

```ts
import { useQueries } from "@tanstack/react-query";
import { getProjectGraph } from "@aq/ts-client";
import { client } from "./client";
import type { MergedGraph, GraphTaskNode, GraphEdge, GraphGate, GraphAgent } from "../pages/command-center/types";

export const projectGraphKey = (pid: string) => ["projectGraph", pid] as const;

export function useProjectGraphs(projectIds: string[]) {
  const results = useQueries({
    queries: projectIds.map((pid) => ({
      queryKey: projectGraphKey(pid),
      queryFn: async () => {
        const r = await getProjectGraph({
          client,
          path: { project_id: pid },
          throwOnError: true,
        });
        return r.data!;
      },
      // Background reconciliation — belt to the WS suspenders.
      refetchInterval: 60_000,
      staleTime: 30_000,
    })),
  });

  const merged: MergedGraph = { tasks: [], edges: [], gates: [], agents: [], taskProject: {} };
  results.forEach((res, i) => {
    if (!res.data) return;
    const pid = projectIds[i];
    for (const t of res.data.tasks as GraphTaskNode[]) {
      merged.tasks.push(t);
      merged.taskProject[t.id] = pid;
    }
    merged.edges.push(...(res.data.edges as GraphEdge[]));
    merged.gates.push(...(res.data.gates as GraphGate[]));
    merged.agents.push(...(res.data.agents as GraphAgent[]));
  });

  return {
    data: merged,
    isLoading: results.some((r) => r.isLoading),
    errors: results.map((r) => (r.error as Error | null) ?? null),
  };
}
```

- [ ] **Step 2: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: pass. If `getProjectGraph`'s response type is `unknown`, register a response-model for the OpenAPI operation — but since it is not a `CommandHandler` command, register it via a FastAPI `response_model` (already done). If types are still `unknown`, ensure the generator picked up the schema by re-running Task 3.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/api/graph.ts
git commit -m "feat(dashboard): useProjectGraphs merges per-project graph snapshots"
```

---

## Task 8: Frontend — WS live-update patcher

**Files:**
- Create: `dashboard/src/pages/command-center/useGraphLive.ts`

**Interfaces:**
- Consumes: `useEventStream`, `useQueryClient`, `NotifyEvent` union, `projectGraphKey`.
- Produces: `useGraphLive(projectIds: string[]): void` — subscribes and mutates `["projectGraph", pid]` caches in place.

- [ ] **Step 1: Write useGraphLive.ts**

Create `dashboard/src/pages/command-center/useGraphLive.ts`:

```ts
import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { ProjectGraphResponse } from "@aq/ts-client";
import { useEventStream } from "../../ws/useEventStream";
import type { NotifyEvent } from "../../ws/types";
import { projectGraphKey } from "../../api/graph";

type Snapshot = ProjectGraphResponse;

/** Incrementally mutate cached per-project graph snapshots on WS events. */
export function useGraphLive(projectIds: string[]) {
  const qc = useQueryClient();

  const patchTask = useCallback(
    (pid: string, taskId: string, patch: Partial<Snapshot["tasks"][number]>) => {
      qc.setQueryData<Snapshot>(projectGraphKey(pid), (prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          tasks: prev.tasks.map((t) => (t.id === taskId ? { ...t, ...patch } : t)),
        };
      });
    },
    [qc],
  );

  const onEvent = useCallback((ev: NotifyEvent) => {
    const type = ev.event_type;

    // task.blocked / task.unblocked (work-graph events) — restyle only.
    if (type === "task.blocked" || type === "task.unblocked") {
      const pid = (ev as { project_id?: string }).project_id;
      const tid = (ev as { task_id?: string }).task_id;
      if (!pid || !tid || !projectIds.includes(pid)) return;
      patchTask(pid, tid, { is_blocked: type === "task.blocked" });
      return;
    }

    // Task lifecycle events carry the full Task shape.
    if (
      type === "notify.task_started" ||
      type === "notify.task_completed" ||
      type === "notify.task_failed" ||
      type === "notify.task_stopped" ||
      type === "notify.task_blocked"
    ) {
      const t = (ev as { task?: { id: string; project_id: string; status: string; assigned_agent?: string | null } }).task;
      if (!t || !projectIds.includes(t.project_id)) return;
      patchTask(t.project_id, t.id, {
        status: t.status,
        assigned_agent_id: t.assigned_agent ?? null,
      });
      return;
    }

    // Gate events: for correctness prefer a targeted refetch of the one project.
    // (Gate rows carry gate_id but not project_id in the current schema; cheapest
    // safe fallback is a per-project invalidate.)
    if (type === "gate.created" || type === "gate.resolved" || type === "gate.expired") {
      for (const pid of projectIds) {
        qc.invalidateQueries({ queryKey: projectGraphKey(pid), exact: true });
      }
      return;
    }

    // Session lifecycle: reflect agent presence next to a task.
    if (type === "session.started" || type === "session.exited" || type === "session.adopted") {
      const pid = (ev as { project_id?: string }).project_id;
      if (!pid || !projectIds.includes(pid)) return;
      qc.invalidateQueries({ queryKey: projectGraphKey(pid), exact: true });
      return;
    }

    // task.created is emitted server-side but is NOT in the current WS union
    // (see dashboard/src/ws/types.ts). Rely on the 60 s reconciliation refetch
    // + the fact that any subsequent task.* event will surface the row.
  }, [patchTask, projectIds, qc]);

  useEventStream({ onEvent });
}
```

- [ ] **Step 2: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/pages/command-center/useGraphLive.ts
git commit -m "feat(dashboard): incremental WS patcher for command-center graph cache"
```

---

## Task 9: Frontend — GraphCanvas component

**Files:**
- Create: `dashboard/src/pages/command-center/GraphCanvas.tsx`

**Interfaces:**
- Consumes: `MergedGraph`, `layoutGraph`, `TaskNode`, `@xyflow/react`.
- Produces: `<GraphCanvas graph merged onTaskClick={(id) => …} />`.

- [ ] **Step 1: Write GraphCanvas.tsx**

Create `dashboard/src/pages/command-center/GraphCanvas.tsx`:

```tsx
import { useMemo } from "react";
import {
  Background, Controls, MiniMap, ReactFlow, ReactFlowProvider,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import TaskNode from "./TaskNode";
import { layoutGraph } from "./layout";
import type { MergedGraph } from "./types";

const nodeTypes = { task: TaskNode };

interface Props {
  graph: MergedGraph;
  onTaskClick: (taskId: string) => void;
}

export default function GraphCanvas({ graph, onTaskClick }: Props) {
  const { nodes, edges } = useMemo(() => layoutGraph(graph), [graph]);

  return (
    <div className="h-full w-full">
      <ReactFlowProvider>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          fitView
          minZoom={0.15}
          maxZoom={2.5}
          onNodeClick={(_, n: Node) => onTaskClick(n.id)}
        >
          <Background gap={24} color="#1f2937" />
          <MiniMap pannable zoomable className="!bg-gray-900" />
          <Controls className="!bg-gray-900 !text-gray-200" />
        </ReactFlow>
      </ReactFlowProvider>
    </div>
  );
}
```

- [ ] **Step 2: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/pages/command-center/GraphCanvas.tsx
git commit -m "feat(dashboard): GraphCanvas React Flow wrapper"
```

---

## Task 10: Frontend — AgentAvatarLayer

**Files:**
- Create: `dashboard/src/pages/command-center/AgentAvatarLayer.tsx`

**Interfaces:**
- Consumes: `agents: GraphAgent[]`, `useReactFlow`.
- Produces: overlay rendered as a child of `<ReactFlow>` — must be integrated inside `GraphCanvas`.

- [ ] **Step 1: Write the overlay**

Create `dashboard/src/pages/command-center/AgentAvatarLayer.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";
import { useReactFlow, useStore } from "@xyflow/react";
import type { GraphAgent } from "./types";

interface Props {
  agents: GraphAgent[];
}

/** Docks each agent avatar to its current task node in screen space.
 *  On current_task_id change, the div's `transform` transitions to the new
 *  projected coordinate — CSS handles the motion.  */
export default function AgentAvatarLayer({ agents }: Props) {
  const rf = useReactFlow();
  // Re-render on viewport (pan/zoom) changes so avatars follow their node.
  const viewport = useStore((s) => s.transform);
  const [_, force] = useState(0);
  const raf = useRef<number | null>(null);
  useEffect(() => {
    // Also re-project after node position updates (layout changes).
    raf.current = requestAnimationFrame(() => force((x) => x + 1));
    return () => { if (raf.current) cancelAnimationFrame(raf.current); };
  }, [viewport, agents]);

  return (
    <div className="pointer-events-none absolute inset-0 z-30">
      {agents.map((a) => {
        if (!a.current_task_id) return null;
        const node = rf.getNode(a.current_task_id);
        if (!node) return null;
        const screen = rf.flowToScreenPosition({
          x: node.position.x + 220 - 8,   // NODE_WIDTH - inset
          y: node.position.y - 8,
        });
        return (
          <div
            key={a.id}
            className="absolute -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white bg-indigo-500 px-1 text-[10px] font-bold text-white shadow"
            style={{
              left: 0, top: 0,
              transform: `translate(${screen.x}px, ${screen.y}px)`,
              transition: "transform 600ms cubic-bezier(0.4, 0, 0.2, 1)",
            }}
            title={`${a.name} — ${a.profile_id ?? ""}`}
          >
            {a.name.slice(0, 2).toUpperCase()}
          </div>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 2: Integrate into GraphCanvas**

Edit `dashboard/src/pages/command-center/GraphCanvas.tsx`. Import `AgentAvatarLayer` and pass `agents` down; render `<AgentAvatarLayer agents={graph.agents} />` **inside** `<ReactFlow>` so `useReactFlow` / `useStore` see the provider.

Replace the current `<ReactFlow …>` body with:

```tsx
<ReactFlow ...>
  <Background gap={24} color="#1f2937" />
  <MiniMap pannable zoomable className="!bg-gray-900" />
  <Controls className="!bg-gray-900 !text-gray-200" />
  <AgentAvatarLayer agents={graph.agents} />
</ReactFlow>
```

- [ ] **Step 3: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/pages/command-center/AgentAvatarLayer.tsx dashboard/src/pages/command-center/GraphCanvas.tsx
git commit -m "feat(dashboard): animated agent avatars docked to task nodes"
```

---

## Task 11: Frontend — ProjectStrip with multi-select

**Files:**
- Create: `dashboard/src/pages/command-center/ProjectStrip.tsx`

**Interfaces:**
- Consumes: `useProjects` (from `dashboard/src/api/hooks.ts`), the merged graph (for vitals).
- Produces:
  ```ts
  <ProjectStrip
    projects={Project[]}
    graph={MergedGraph}
    selected={string[]}
    onToggle={(pid) => void}
  />
  ```

- [ ] **Step 1: Write ProjectStrip.tsx**

Create `dashboard/src/pages/command-center/ProjectStrip.tsx`:

```tsx
import type { MergedGraph } from "./types";

interface Project { id: string; name: string; }

interface Props {
  projects: Project[];
  graph: MergedGraph;
  selected: string[];
  onToggle: (pid: string) => void;
}

function vitals(pid: string, g: MergedGraph) {
  const tasks = g.tasks.filter((t) => g.taskProject[t.id] === pid);
  const running = tasks.filter((t) => t.status === "IN_PROGRESS").length;
  const blocked = tasks.filter((t) => t.is_blocked).length;
  const ready = tasks.filter((t) => t.status === "READY" && !t.is_blocked).length;
  const openGates = g.gates.filter(
    (gt) => gt.status === "open" && gt.task_ids.some((tid) => g.taskProject[tid] === pid),
  ).length;
  return { running, blocked, ready, openGates };
}

export default function ProjectStrip({ projects, graph, selected, onToggle }: Props) {
  return (
    <div className="flex gap-2 overflow-x-auto border-b border-gray-800 bg-gray-950 p-2">
      {projects.map((p) => {
        const v = vitals(p.id, graph);
        const on = selected.includes(p.id);
        return (
          <button
            key={p.id}
            type="button"
            onClick={() => onToggle(p.id)}
            className={`min-w-[180px] rounded border p-2 text-left text-xs ${on ? "border-indigo-500 bg-indigo-950" : "border-gray-800 bg-gray-900 hover:bg-gray-800"}`}
          >
            <div className="mb-1 truncate font-semibold text-gray-100">{p.name}</div>
            <div className="grid grid-cols-4 gap-1 text-center">
              <span title="running"     className="rounded bg-indigo-500/20 py-0.5">{v.running}</span>
              <span title="ready"       className="rounded bg-sky-500/20     py-0.5">{v.ready}</span>
              <span title="blocked"     className="rounded bg-amber-500/20   py-0.5">{v.blocked}</span>
              <span title="open gates"  className="rounded bg-gray-500/20    py-0.5">{v.openGates}</span>
            </div>
          </button>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 2: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/pages/command-center/ProjectStrip.tsx
git commit -m "feat(dashboard): ProjectStrip vitals cards with multi-select"
```

---

## Task 12: Frontend — TaskSidebar (desktop sidebar / mobile bottom sheet)

**Files:**
- Create: `dashboard/src/pages/command-center/TaskSidebar.tsx`

**Interfaces:**
- Consumes: `useTask(taskId)`, `useGateResolve` (from `dashboard/src/api/hooks.ts`), `GraphGate`.
- Produces:
  ```ts
  <TaskSidebar taskId={string | null} gates={GraphGate[]} onClose={() => void} />
  ```

- [ ] **Step 1: Verify existing gate-resolve hook signature**

Run: `grep -n "useGateResolve\|gateResolve" dashboard/src/api/hooks.ts`
Expected: `useGateResolve` mutation exists (line ~1089). If the mutation takes `{ gate_id, decision, ... }`, adapt Step 2 accordingly.

- [ ] **Step 2: Write TaskSidebar.tsx**

Create `dashboard/src/pages/command-center/TaskSidebar.tsx`:

```tsx
import { Link } from "react-router-dom";
import { XMarkIcon, ArrowTopRightOnSquareIcon } from "@heroicons/react/24/outline";
import { useTask } from "../../api/hooks";
import StatusBadge from "../../components/StatusBadge";
import type { GraphGate } from "./types";

interface Props {
  taskId: string | null;
  gates: GraphGate[];
  onResolveGate: (gateId: string, decision: "approve" | "reject") => void;
  onClose: () => void;
}

export default function TaskSidebar({ taskId, gates, onResolveGate, onClose }: Props) {
  const { data: task } = useTask(taskId ?? undefined);
  if (!taskId) return null;
  const taskGates = gates.filter((g) => g.task_ids.includes(taskId));

  return (
    <aside className="fixed inset-x-0 bottom-0 z-40 flex max-h-[75vh] flex-col overflow-y-auto border-t border-gray-800 bg-gray-950 p-4 md:right-0 md:top-0 md:bottom-0 md:left-auto md:max-h-none md:w-[420px] md:border-l md:border-t-0">
      <header className="mb-3 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate font-mono text-xs text-gray-500">{taskId}</p>
          <h2 className="truncate text-lg font-semibold text-gray-100">
            {task?.title ?? "Loading…"}
          </h2>
          <StatusBadge status={task?.status ?? ""} />
        </div>
        <button type="button" onClick={onClose} className="rounded p-1 text-gray-400 hover:text-gray-200">
          <XMarkIcon className="h-5 w-5" />
        </button>
      </header>

      {task?.description && (
        <section className="mb-3 whitespace-pre-wrap rounded border border-gray-800 bg-gray-900 p-2 text-sm text-gray-300">
          {task.description}
        </section>
      )}

      <section className="mb-3 flex flex-wrap gap-1 text-xs">
        {task?.profile_id && <span className="rounded bg-gray-800 px-2 py-0.5">{task.profile_id}</span>}
        {(task as { intelligence_class?: string })?.intelligence_class && (
          <span className="rounded bg-gray-800 px-2 py-0.5">
            {(task as { intelligence_class?: string }).intelligence_class}
          </span>
        )}
      </section>

      {taskGates.length > 0 && (
        <section className="mb-3">
          <h3 className="mb-1 text-xs uppercase text-gray-400">Gates</h3>
          <ul className="space-y-1">
            {taskGates.map((g) => (
              <li key={g.id} className="flex items-center justify-between rounded border border-gray-800 bg-gray-900 p-2 text-sm">
                <span>{g.gate_type} <span className="text-xs text-gray-500">{g.status}</span></span>
                {g.gate_type === "human" && g.status === "open" && (
                  <span className="flex gap-1">
                    <button onClick={() => onResolveGate(g.id, "approve")}
                            className="rounded bg-emerald-600 px-2 py-0.5 text-xs">Approve</button>
                    <button onClick={() => onResolveGate(g.id, "reject")}
                            className="rounded bg-red-600 px-2 py-0.5 text-xs">Reject</button>
                  </span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {(task?.depends_on ?? []).length > 0 && (
        <section className="mb-3">
          <h3 className="mb-1 text-xs uppercase text-gray-400">Depends on</h3>
          <ul className="space-y-0.5 text-xs">
            {task!.depends_on.map((d) => (
              <li key={d.id}>
                <Link to={`/tasks/${d.id}`} className="font-mono text-indigo-400">{d.id}</Link>
                <span className="ml-2 text-gray-400">{d.title}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {task?.pr_url && (
        <a href={task.pr_url} target="_blank" rel="noreferrer"
           className="mb-3 inline-flex items-center gap-1 text-sm text-indigo-400">
          PR <ArrowTopRightOnSquareIcon className="h-4 w-4" />
        </a>
      )}

      <Link to={`/tasks/${taskId}`} className="mt-auto text-center text-sm text-indigo-400">
        Open full task detail →
      </Link>
    </aside>
  );
}
```

- [ ] **Step 3: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: pass. If `useTask` requires a defined string (not `undefined`), gate the hook with `taskId ?? ""` and guard rendering on `taskId`.

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/pages/command-center/TaskSidebar.tsx
git commit -m "feat(dashboard): TaskSidebar with inline gate resolve + mobile bottom sheet"
```

---

## Task 13: Frontend — GhostOverlay stub with feature detection

**Files:**
- Create: `dashboard/src/pages/command-center/GhostOverlay.tsx`

**Interfaces:**
- Consumes: `proposalId?: string`, `useReactFlow`.
- Produces: `<GhostOverlay proposalId={string | null} />` — renders `null` on 404 or missing endpoint.

- [ ] **Step 1: Write the overlay**

Create `dashboard/src/pages/command-center/GhostOverlay.tsx`:

```tsx
import { useQuery } from "@tanstack/react-query";

interface ProposalResponse {
  proposal_id: string;
  tasks: Array<{ id: string; title: string }>;
  edges: Array<{ from: string; to: string; dep_type: string }>;
  status: string;
}

interface Props { proposalId: string | null; }

/** Ghost preview of a Phase 6 task-batch proposal.  Feature-detects the
 *  endpoint — if the server returns 404 (Phase 6 not deployed), renders
 *  nothing and stays silent.  */
export default function GhostOverlay({ proposalId }: Props) {
  const { data } = useQuery<ProposalResponse | null>({
    queryKey: ["proposal", proposalId],
    enabled: !!proposalId,
    retry: false,
    queryFn: async () => {
      if (!proposalId) return null;
      const r = await fetch(`/api/proposals/${proposalId}`);
      if (r.status === 404) return null;
      if (!r.ok) throw new Error(`proposal fetch ${r.status}`);
      return (await r.json()) as ProposalResponse;
    },
  });

  if (!data) return null;
  return (
    <div className="pointer-events-none absolute inset-0 z-20">
      {data.tasks.map((t, i) => (
        <div key={t.id}
             className="absolute rounded border-2 border-dashed border-fuchsia-400/60 bg-fuchsia-500/5 p-1 text-[10px] text-fuchsia-200"
             style={{ left: 40 + i * 240, top: 40, width: 220 }}>
          {t.title}
        </div>
      ))}
    </div>
  );
}
```

Note: this is the one exception to the "no direct `fetch`" rule — Phase 6 hasn't landed the endpoint, so it isn't in the SDK yet. When Phase 6 ships the endpoint and regenerates the client, swap this to the generated call.

- [ ] **Step 2: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/pages/command-center/GhostOverlay.tsx
git commit -m "feat(dashboard): GhostOverlay with 404 feature-detection for Phase 6"
```

---

## Task 14: Frontend — mobile card-list fallback

**Files:**
- Create: `dashboard/src/pages/command-center/MobileCardList.tsx`

**Interfaces:**
- Consumes: `graph: MergedGraph`, `onTaskClick`.

- [ ] **Step 1: Write MobileCardList.tsx**

Create `dashboard/src/pages/command-center/MobileCardList.tsx`:

```tsx
import type { MergedGraph } from "./types";

interface Props {
  graph: MergedGraph;
  onTaskClick: (taskId: string) => void;
}

const BUCKETS = ["IN_PROGRESS", "READY", "BLOCKED", "DEFINED", "FAILED", "COMPLETED"];

export default function MobileCardList({ graph, onTaskClick }: Props) {
  return (
    <div className="space-y-4 p-2">
      {BUCKETS.map((status) => {
        const tasks = graph.tasks.filter((t) => t.status === status);
        if (tasks.length === 0) return null;
        return (
          <section key={status}>
            <h3 className="mb-1 text-xs uppercase text-gray-400">{status} ({tasks.length})</h3>
            <ul className="space-y-1">
              {tasks.map((t) => (
                <li key={t.id}>
                  <button type="button" onClick={() => onTaskClick(t.id)}
                          className="w-full rounded border border-gray-800 bg-gray-900 p-2 text-left text-sm">
                    <div className="truncate">{t.title}</div>
                    <div className="text-xs text-gray-500">{t.profile_id ?? "—"}</div>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 2: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/pages/command-center/MobileCardList.tsx
git commit -m "feat(dashboard): mobile status-grouped card list fallback"
```

---

## Task 15: Frontend — CommandCenter page + routing + orientation gate

**Files:**
- Create: `dashboard/src/pages/CommandCenter.tsx`
- Modify: `dashboard/src/App.tsx`
- Modify: `dashboard/src/components/Sidebar.tsx` (add nav link if not present)

**Interfaces:**
- Consumes: everything above.
- Produces: `<CommandCenter />` at `/command-center`.

- [ ] **Step 1: Write CommandCenter.tsx**

Create `dashboard/src/pages/CommandCenter.tsx`:

```tsx
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { gateResolve } from "@aq/ts-client";
import { client } from "../api/client";
import { useProjects } from "../api/hooks";
import { useProjectGraphs, projectGraphKey } from "../api/graph";
import GraphCanvas from "./command-center/GraphCanvas";
import ProjectStrip from "./command-center/ProjectStrip";
import TaskSidebar from "./command-center/TaskSidebar";
import GhostOverlay from "./command-center/GhostOverlay";
import MobileCardList from "./command-center/MobileCardList";
import { useGraphLive } from "./command-center/useGraphLive";

const SELECTED_KEY = "aq:command-center:selected";

function useIsMobile() {
  const [m, setM] = useState(() => window.matchMedia("(max-width: 768px)").matches);
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 768px)");
    const onC = (e: MediaQueryListEvent) => setM(e.matches);
    mq.addEventListener("change", onC);
    return () => mq.removeEventListener("change", onC);
  }, []);
  return m;
}

function useIsLandscape() {
  const [l, setL] = useState(() => window.matchMedia("(orientation: landscape)").matches);
  useEffect(() => {
    const mq = window.matchMedia("(orientation: landscape)");
    const onC = (e: MediaQueryListEvent) => setL(e.matches);
    mq.addEventListener("change", onC);
    return () => mq.removeEventListener("change", onC);
  }, []);
  return l;
}

export default function CommandCenter() {
  const { data: projects = [] } = useProjects();
  const [selected, setSelected] = useState<string[]>(() => {
    try { return JSON.parse(localStorage.getItem(SELECTED_KEY) ?? "[]"); }
    catch { return []; }
  });
  useEffect(() => {
    localStorage.setItem(SELECTED_KEY, JSON.stringify(selected));
  }, [selected]);

  // Auto-select first project if none.
  useEffect(() => {
    if (selected.length === 0 && projects.length > 0) {
      setSelected([projects[0].id]);
    }
  }, [projects, selected.length]);

  const { data: graph, isLoading } = useProjectGraphs(selected);
  useGraphLive(selected);

  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const isMobile = useIsMobile();
  const isLandscape = useIsLandscape();
  const showCanvas = !isMobile || isLandscape;

  const qc = useQueryClient();
  const resolveMut = useMutation({
    mutationFn: async (input: { gate_id: string; decision: "approve" | "reject" }) => {
      const r = await gateResolve({ client, body: input as never, throwOnError: true });
      return r.data;
    },
    onSuccess: () => {
      for (const pid of selected) {
        qc.invalidateQueries({ queryKey: projectGraphKey(pid), exact: true });
      }
    },
  });

  const toggle = (pid: string) =>
    setSelected((prev) => prev.includes(pid) ? prev.filter((x) => x !== pid) : [...prev, pid]);

  const proposalId = useMemo(() => {
    const params = new URLSearchParams(window.location.search);
    return params.get("proposal");
  }, []);

  return (
    <div className="flex h-[calc(100vh-4rem)] flex-col">
      <ProjectStrip projects={projects} graph={graph} selected={selected} onToggle={toggle} />
      <div className="relative flex-1 overflow-hidden">
        {isLoading ? (
          <p className="p-4 text-sm text-gray-400">Loading graph…</p>
        ) : showCanvas ? (
          <>
            <GraphCanvas graph={graph} onTaskClick={setSelectedTaskId} />
            <GhostOverlay proposalId={proposalId} />
          </>
        ) : (
          <MobileCardList graph={graph} onTaskClick={setSelectedTaskId} />
        )}
      </div>
      <TaskSidebar
        taskId={selectedTaskId}
        gates={graph.gates}
        onResolveGate={(gid, dec) => resolveMut.mutate({ gate_id: gid, decision: dec })}
        onClose={() => setSelectedTaskId(null)}
      />
    </div>
  );
}
```

- [ ] **Step 2: Add the route**

Edit `dashboard/src/App.tsx`. Import `CommandCenter` and add a route under the top-level `<Layout />` block, before the wildcard:

```tsx
import CommandCenter from "./pages/CommandCenter";
// ...
<Route path="command-center" element={<CommandCenter />} />
```

If a Phase 3 placeholder already exists at this path, **replace** its element with `<CommandCenter />` and remove the placeholder component import.

- [ ] **Step 3: Add / verify Sidebar link**

Open `dashboard/src/components/Sidebar.tsx`. If a "Command Center" link is missing, add:

```tsx
<NavLink to="/command-center" className={navLinkClass}>Command Center</NavLink>
```

Use the file's existing helper class / component. If Phase 3 already added it, this step is a no-op.

- [ ] **Step 4: Typecheck + lint**

Run: `cd dashboard && npm run typecheck && npm run lint`
Expected: both pass.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/pages/CommandCenter.tsx dashboard/src/App.tsx dashboard/src/components/Sidebar.tsx
git commit -m "feat(dashboard): Command Center page — canvas, live updates, sidebar"
```

---

## Task 16: Manual verification

**Files:** none — verification only.

- [ ] **Step 1: Run backend + frontend**

Run: `./run.sh start` (daemon on 8081)
Run in a second terminal: `cd dashboard && npm run dev`
Open http://localhost:5173/command-center.

- [ ] **Step 2: Desktop smoke**

- Project strip renders every project card with 4 vitals numbers.
- Click a project card — canvas populates with pan/zoom-able nodes.
- Multi-select two projects — both graphs render; a cross-project edge (if any exists) appears when both ends are loaded, and disappears when one project is deselected.
- Create a task via CLI (`aq task create --project <p> --title 'test node'`) — new node appears within 60 s (reconciliation refetch) OR immediately after any `task.*` event fires.
- Trigger a task to IN_PROGRESS — spinner ring appears; completing it flashes green.
- An assigned agent's avatar shows on the task's node; forcing a reassignment (`aq agent reassign …`) animates the avatar to the new node over ~600 ms.
- Click a task — sidebar slides in; description, gates, deps show; if a human gate exists, Approve/Reject resolve it and the badge disappears.

- [ ] **Step 3: Mobile smoke**

- Chrome DevTools → Toggle device toolbar → iPhone 14 (portrait).
- Portrait: `MobileCardList` renders; canvas hidden.
- Rotate to landscape: canvas renders with pinch-zoom.
- Tap a card → bottom sheet appears; close (X) dismisses.

- [ ] **Step 4: Verify WS invalidation is surgical**

Chrome DevTools → Network → filter `graph`. Trigger a `task.blocked` event (e.g. add a dependency). Expect: **no** new `GET /api/projects/…/graph` request (in-place patch). Wait 60 s: expect exactly one background refetch.

- [ ] **Step 5: Backend test run**

Run: `pytest tests/test_api_graph.py -v`
Expected: PASS.

- [ ] **Step 6: Full frontend gate**

Run: `cd dashboard && npm run typecheck && npm run lint && npm run build`
Expected: all three succeed.

- [ ] **Step 7: Commit any verification-driven fixups**

If Step 2–4 uncovered anything (e.g. avatars stuttering on pan), fix and commit as separate `fix(dashboard): …` commits.

---

## Self-Review

- **Spec coverage §9.2** — project strip ✅ (Task 11), canvas ✅ (Task 9), status color / spinner / completion flash / priority ring / gate badges ✅ (Task 6), edge styles by dep_type ✅ (Task 5), ghost overlay ✅ (Task 13), agent avatars docked+animated ✅ (Task 10), WS-driven updates ✅ (Task 8).
- **§9.3** — task sidebar with routing chips, gates + inline resolve, deps, PR link ✅ (Task 12). (Agent-panel/live-tmux is Phase 5 per spec §12.5, not in scope.)
- **§12.4** — endpoint owned by Phase 4 ✅ (Tasks 1–3).
- **Multi-project + cross-project edges** ✅ (Tasks 5, 7).
- **Mobile degradation** ✅ (Tasks 14, 15).
- **Ghost overlay feature-detects** ✅ (Task 13).
- **No placeholders** — every step has runnable code or exact commands. Type names (`MergedGraph`, `GraphTaskNode`, `projectGraphKey`, `useGraphLive`, `layoutGraph`) are consistent across tasks.
- **Test-first for the backend** — Task 1 writes tests first; Task 2 makes them pass.
- **Frontend testing gap declared** — `dashboard/` has no unit-test runner in `package.json`; verification leans on typecheck + lint + build + manual smoke (Task 16).

## Open Questions

1. **task.created event.** The current WS union in `dashboard/src/ws/types.ts` doesn't include a `task.created` frame — new tasks land via the 60 s reconciliation. If the backend already emits one under another name, we can wire it in Task 8; otherwise a follow-up spec should add it. **Recommend: check with backend owner before merge.**
2. **`session_id` on Agent.** `AgentQueryMixin._row_to_agent` doesn't currently set `session_id`; the field may need to be added to the `agents` table or joined from `sessions` in the router. Task 2 falls back to `getattr(a, "session_id", None)` — safe but always `None` today.
3. **Gate `project_id`.** Gate rows carry `project_id`, so gate WS events *could* include it and let us patch only the affected project's cache without invalidation. Confirm with backend owner; if added, tighten Task 8's gate branch.
4. **Cross-project edge storage.** `task_dependencies.dep_type` filtering assumes edges live in the same table regardless of task project. Verified via `src/database/queries/dependency_queries.py` — no project filter on the edges table. Nothing to change; just noting the assumption.

---

**Plan complete and saved to `docs/superpowers/plans/2026-08-21-dv2-phase4-command-center.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
