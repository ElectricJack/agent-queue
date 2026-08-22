# Pane View: `task-detail` (v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `task-detail` pane view — the shell's `<ShellPane>` projection of a single task's status, actions, metadata, and relationships — so the Command Center graph, task tables, agent rows, and agent-pushed chat messages can all open a task without navigating away from what the user is doing.

**Architecture:** A self-contained directory `dashboard/src/panes/task-detail/` implementing the pane-plugin contract (manifest + component typed against `PaneViewProps`). This plan also bootstraps the shared pane infrastructure that no pane view has created yet: `dashboard/src/panes/types.ts` (the `PaneManifest`/`PaneViewProps`/`PaneToolbarAction`/`ShortcutBinding` contracts), `dashboard/src/panes/registry.ts` (build-time view registry) with its parity test, and `src/panes/registry.py` (server-side mirror validated by a pytest parity test). Frontend test infrastructure (Vitest + React Testing Library) and the `zod` dependency do not exist in this workspace yet and are installed as part of Task 1. `TaskSidebar.tsx` retirement and the graph's node-click wiring happen in this plan's last implementation task, matching the pane spec's explicit inclusion of that retirement in its own scope.

**Tech Stack:** React 19, TypeScript, TanStack Query, Tailwind v4, `@heroicons/react/24/outline`, `zod` (new dependency), Vitest + `@testing-library/react` + `@testing-library/jest-dom` + `jsdom` (new dev dependencies), `react-router-dom` v7, Python 3.12 + pytest (backend mirror + parity test).

**Spec:**
- `docs/superpowers/specs/2026-08-22-dashboard-shell-v2-design.md` (shell contract — `<ShellPane>` §5, Command Center graph §7.4)
- `docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md` (pane plugin contract every view implements)
- `docs/superpowers/specs/2026-08-22-pane-task-detail-design.md` (this view's spec)

## Global Constraints

Copied verbatim (or paraphrased where the source is prose) from the specs above. Every task's requirements implicitly include this section.

- Icons: `@heroicons/react/24/outline` only. `LucideIcon` must NOT be introduced (interface spec §4, note on `icon`).
- Every pane view exports a default component matching `PaneViewProps<TArgs>`: `{ args, close, setArgs, setToolbar, setShortcuts }` (interface spec §5).
- `args_schema` is a zod schema; args are runtime-validated on every `open()` call — invalid args fail loudly (console.error + no-op), never render a broken pane (interface spec §4, §6.1).
- Tests: Vitest + React Testing Library for the dashboard; no E2E infra in this repo (shell spec §9.2).
- `manifest.id` must match the directory name exactly and be unique across the registry (interface spec §4.2).
- `open_shortcut`, if present, must not collide with another view's `open_shortcut` or a reserved shell shortcut (interface spec §4.2) — this view has none, so N/A, but the registry-level check still applies generally.
- Not replacing `/tasks/:id` — editing, Explain, and Graph tabs stay full-page-only; "edit" always routes to the full page (task-detail spec §2).
- Not changing `TaskActions.tsx` or its mutation hooks — this view is a new consumer, not a new implementation (task-detail spec §2).
- Not building the `InlineEventCard` `pane_open` chip — shared shell work, out of scope here (task-detail spec §2).
- Not adding a task entity picker for the palette action (task-detail spec §2).
- No changes to `TaskDetail.tsx` from this plan — retiring `TaskSidebar.tsx` is explicitly in scope (task-detail spec §1), but `TaskDetail.tsx` itself is untouched (task-detail spec §11 checklist).

### Prerequisite: Phase B shell primitive (`useShellPane`)

Per the shell spec, `<ShellPane>` and its store ship in a prior phase ("Phase B — shell foundation") at `dashboard/src/shell/`. This view imports `useShellPane()` from there for pane-internal navigation (task-detail spec §5.4: clicking a related task calls `open("task-detail", { taskId })` again). At the time this plan was written, `dashboard/src/shell/` does not exist in this repository. Task 3, Step 0 creates a minimal placeholder matching the exact contract the interface spec defines (interface spec §6.1: `{ open, close, state }`) so this plan's tasks are self-testable independent of Phase B's landing order. If `dashboard/src/shell/useShellPane.ts` already exists with this export shape by the time Task 3 runs, skip that step entirely and use the real module.

---

## Task 1: Pane directory skeleton, shared pane types, manifest, and test infrastructure bootstrap

**Files:**
- Create: `dashboard/src/panes/types.ts`
- Create: `dashboard/src/panes/task-detail/manifest.ts`
- Create: `dashboard/src/panes/task-detail/__tests__/manifest.test.ts`
- Create: `dashboard/vitest.config.ts`
- Create: `dashboard/src/test/setup.ts`
- Modify: `dashboard/package.json` (add `zod`, `vitest`, `@testing-library/react`, `@testing-library/jest-dom`, `@testing-library/user-event`, `jsdom` deps; add `"test": "vitest run"` script)

**Interfaces:**
- Consumes: nothing (first task).
- Produces:
  - `PaneManifest<TArgs = unknown>` interface (id, name, description, icon, args_schema?, open_shortcut?, route_scope?, agent_pushable?, palette_label?, palette_section?) — exported from `dashboard/src/panes/types.ts`, consumed by every later task's manifest and by Task 5's registry.
  - `PaneViewProps<TArgs = unknown>` interface (args, close, setArgs, setToolbar, setShortcuts) — exported from `dashboard/src/panes/types.ts`, consumed by Task 3.
  - `PaneToolbarAction` interface (id, label, icon?, onClick, disabled?) — exported from `dashboard/src/panes/types.ts`, consumed by Task 6.
  - `ShortcutBinding` interface (key, label, onFire) — exported from `dashboard/src/panes/types.ts`, consumed by Task 6.
  - `PaneEntry` interface (manifest, Component) — exported from `dashboard/src/panes/types.ts`, consumed by Task 5.
  - `manifest: PaneManifest<TaskDetailArgs>` and `taskDetailArgsSchema: z.ZodType<TaskDetailArgs>` — exported from `dashboard/src/panes/task-detail/manifest.ts`, consumed by Tasks 3, 5, 8, 10.

- [ ] **Step 1: Install new dependencies**

```bash
cd /home/jkern/dev/agent-queue2/dashboard
npm install zod
npm install -D vitest @testing-library/react @testing-library/jest-dom @testing-library/user-event jsdom
```

- [ ] **Step 2: Verify install**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && node -e "require('zod'); require('vitest'); console.log('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Write the test-infra config (no test yet — this is scaffolding the rest of the plan depends on)**

`dashboard/src/test/setup.ts`:

```ts
import "@testing-library/jest-dom/vitest";
```

`dashboard/vitest.config.ts`:

```ts
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: true,
  },
});
```

Add to `dashboard/package.json` `"scripts"`:

```json
"test": "vitest run"
```

- [ ] **Step 4: Write the failing manifest test**

`dashboard/src/panes/task-detail/__tests__/manifest.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { manifest, taskDetailArgsSchema } from "../manifest";

describe("task-detail manifest", () => {
  it("has id matching the directory name", () => {
    expect(manifest.id).toBe("task-detail");
  });

  it("accepts a valid taskId", () => {
    const result = taskDetailArgsSchema.safeParse({ taskId: "abc-123" });
    expect(result.success).toBe(true);
  });

  it("rejects an empty taskId", () => {
    const result = taskDetailArgsSchema.safeParse({ taskId: "" });
    expect(result.success).toBe(false);
  });

  it("rejects missing taskId", () => {
    const result = taskDetailArgsSchema.safeParse({});
    expect(result.success).toBe(false);
  });

  it("has no open_shortcut", () => {
    expect(manifest.open_shortcut).toBeUndefined();
  });

  it("is agent-pushable with the Task palette section", () => {
    expect(manifest.agent_pushable).toBe(true);
    expect(manifest.palette_label).toBe("Open task");
    expect(manifest.palette_section).toBe("Task");
  });
});
```

- [ ] **Step 5: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/task-detail/__tests__/manifest.test.ts`
Expected: FAIL — `Cannot find module '../manifest'` (neither `types.ts` nor `manifest.ts` exist yet).

- [ ] **Step 6: Write `dashboard/src/panes/types.ts`**

```ts
import type { ComponentType, SVGProps } from "react";
import type { z } from "zod";

/** Heroicons outline-icon component type used everywhere in dashboard/src/. */
export type HeroIcon = ComponentType<SVGProps<SVGSVGElement>>;

export interface PaneManifest<TArgs = unknown> {
  id: string;
  name: string;
  description: string;
  icon: HeroIcon;
  args_schema?: z.ZodType<TArgs>;
  open_shortcut?: string;
  route_scope?: "cross-route" | "route-scoped";
  agent_pushable?: boolean;
  palette_label?: string | null;
  palette_section?: string;
}

export interface PaneToolbarAction {
  id: string;
  label: string;
  icon?: HeroIcon;
  onClick: () => void;
  disabled?: boolean;
}

export interface ShortcutBinding {
  key: string;
  label: string;
  onFire: () => void;
}

export interface PaneViewProps<TArgs = unknown> {
  args: TArgs;
  close: () => void;
  setArgs: (next: TArgs) => void;
  setToolbar: (actions: PaneToolbarAction[]) => void;
  setShortcuts: (bindings: ShortcutBinding[]) => void;
}

export interface PaneEntry {
  manifest: PaneManifest;
  Component: ComponentType<PaneViewProps>;
}
```

- [ ] **Step 7: Write `dashboard/src/panes/task-detail/manifest.ts`**

```ts
import { z } from "zod";
import { ClipboardDocumentListIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";

export const taskDetailArgsSchema = z.object({
  taskId: z.string().min(1),
});

export type TaskDetailArgs = z.infer<typeof taskDetailArgsSchema>;

export const manifest: PaneManifest<TaskDetailArgs> = {
  id: "task-detail",
  name: "Task",
  description: "Task status, actions, metadata, and relationships.",
  icon: ClipboardDocumentListIcon,
  args_schema: taskDetailArgsSchema,
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Open task",
  palette_section: "Task",
};
```

- [ ] **Step 8: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/task-detail/__tests__/manifest.test.ts`
Expected: PASS — 6 tests.

- [ ] **Step 9: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/package.json dashboard/package-lock.json dashboard/vitest.config.ts \
  dashboard/src/test/setup.ts dashboard/src/panes/types.ts \
  dashboard/src/panes/task-detail/manifest.ts \
  dashboard/src/panes/task-detail/__tests__/manifest.test.ts
git commit -m "feat(dashboard): pane types + task-detail manifest + Vitest infra bootstrap"
```

---

## Task 2: Server-side registry mirror entry (`src/panes/registry.py`)

**Files:**
- Create: `src/panes/__init__.py`
- Create: `src/panes/registry.py`
- Test: `tests/test_pane_registry.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SERVER_PANE_REGISTRY: dict[str, dict[str, object]]` — a module-level dict mapping view id to `{"agent_pushable": bool}`, exported from `src/panes/registry.py`. Consumed by Task 10's parity test.

- [ ] **Step 1: Write the failing test**

`tests/test_pane_registry.py`:

```python
from src.panes.registry import SERVER_PANE_REGISTRY


def test_task_detail_is_registered_and_agent_pushable():
    assert "task-detail" in SERVER_PANE_REGISTRY
    assert SERVER_PANE_REGISTRY["task-detail"]["agent_pushable"] is True


def test_registry_values_have_agent_pushable_bool():
    for view_id, entry in SERVER_PANE_REGISTRY.items():
        assert isinstance(view_id, str) and view_id
        assert isinstance(entry["agent_pushable"], bool)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2 && pytest tests/test_pane_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.panes'`.

- [ ] **Step 3: Write the implementation**

`src/panes/__init__.py`:

```python
"""Server-side mirror of the frontend pane-view registry.

Source of truth for the frontend side is
``dashboard/src/panes/registry.ts`` (interface spec §4.1). This package
keeps a hand-maintained mirror so the daemon can validate `--pane-open`
message frames (interface spec §6.5, §7) without needing to parse
TypeScript. ``tests/test_pane_registry_parity.py`` asserts the two stay
in sync; that test lands with the second pane view (whichever view isn't
this one) once a second view exists to compare against.
"""
```

`src/panes/registry.py`:

```python
"""Static mirror of `dashboard/src/panes/registry.ts` (pane-plugin
interface spec §7, option A). Add one entry here per pane view that
ships; keep in sync by hand — a parity test (`tests/test_pane_registry_parity.py`,
added once a second pane view exists) enforces this against the frontend
registry.
"""

from __future__ import annotations

SERVER_PANE_REGISTRY: dict[str, dict[str, object]] = {
    "task-detail": {"agent_pushable": True},
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2 && pytest tests/test_pane_registry.py -v`
Expected: PASS — 2 tests.

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add src/panes/__init__.py src/panes/registry.py tests/test_pane_registry.py
git commit -m "feat(panes): server-side pane registry mirror with task-detail entry"
```

---

## Task 3: Component — header, description, TaskActions bar

**Files:**
- Create: `dashboard/src/shell/useShellPane.ts` (Phase B placeholder — see Global Constraints prerequisite note; skip this file if it already exists with the described export shape)
- Create: `dashboard/src/panes/task-detail/index.tsx`
- Create: `dashboard/src/panes/task-detail/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `manifest`, `taskDetailArgsSchema`, `TaskDetailArgs` from `dashboard/src/panes/task-detail/manifest.ts` (Task 1); `PaneViewProps` from `dashboard/src/panes/types.ts` (Task 1); `useTask` and `Task` type from `dashboard/src/api/hooks.ts:276`; `TaskActions` default export from `dashboard/src/components/TaskActions.tsx`; `StatusBadge` default export from `dashboard/src/components/StatusBadge.tsx`; `useShellPane` from `dashboard/src/shell/useShellPane.ts`.
- Produces: `TaskDetailPane` default export from `dashboard/src/panes/task-detail/index.tsx`, typed `PaneViewProps<TaskDetailArgs>` — the component further built out in Tasks 4–6, consumed by Task 5's registry.

- [ ] **Step 0 (only if `dashboard/src/shell/useShellPane.ts` does not already exist): write the Phase B placeholder**

`dashboard/src/shell/useShellPane.ts`:

```ts
/**
 * PLACEHOLDER — Phase B (shell foundation, dashboard-shell-v2-design.md §5)
 * owns the real `<ShellPane>` primitive and store. This minimal stand-in
 * exists so Phase C pane views (this repo doesn't yet contain Phase B) can
 * be built and tested against the exact contract the interface spec
 * defines (§6.1: `{ open, close, state }`). Replace this file wholesale
 * when Phase B lands; do not extend it with shell-only concerns (width,
 * drawer-closing, localStorage) — those belong to the real
 * `useShellPaneStore`.
 */
import { useSyncExternalStore } from "react";

export type PaneState =
  | { kind: "closed" }
  | { kind: "open"; view: string; args: unknown; width: number };

let state: PaneState = { kind: "closed" };
const listeners = new Set<() => void>();

function setState(next: PaneState) {
  state = next;
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot(): PaneState {
  return state;
}

export function useShellPane() {
  const snapshot = useSyncExternalStore(subscribe, getSnapshot);
  return {
    state: snapshot,
    open: (view: string, args: unknown) => setState({ kind: "open", view, args, width: 480 }),
    close: () => setState({ kind: "closed" }),
  };
}
```

- [ ] **Step 1: Write the failing test**

`dashboard/src/panes/task-detail/__tests__/index.test.tsx`:

```tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import TaskDetailPane from "../index";
import type { Task } from "../../../api/hooks";

const mockUseTask = vi.fn();
const mockUseGates = vi.fn();
const mockUseResolveGate = vi.fn();

vi.mock("../../../api/hooks", async () => {
  const actual = await vi.importActual<typeof import("../../../api/hooks")>(
    "../../../api/hooks",
  );
  return {
    ...actual,
    useTask: (...args: unknown[]) => mockUseTask(...args),
    useGates: (...args: unknown[]) => mockUseGates(...args),
    useResolveGate: (...args: unknown[]) => mockUseResolveGate(...args),
  };
});

vi.mock("../../../shell/useShellPane", () => ({
  useShellPane: () => ({ open: vi.fn(), close: vi.fn(), state: { kind: "closed" } }),
}));

const fixtureTask: Task = {
  id: "t1",
  project_id: "demo",
  title: "Fix the thing",
  description: "",
  status: "AWAITING_APPROVAL",
  priority: 2,
  assigned_agent: "agent-1",
  retry_count: 0,
  max_retries: 3,
  requires_approval: true,
  is_plan_subtask: false,
  task_type: "implementation",
  profile_id: "claude-sdk",
  auto_approve_plan: false,
  skip_verification: false,
  pr_url: null,
  depends_on: [],
  blocks: [],
  subtasks: [],
  created_at: 1755878400,
  updated_at: 1755878400,
};

function noopProps() {
  return {
    args: { taskId: "t1" },
    close: vi.fn(),
    setArgs: vi.fn(),
    setToolbar: vi.fn(),
    setShortcuts: vi.fn(),
  };
}

beforeEach(() => {
  mockUseTask.mockReset();
  mockUseGates.mockReset();
  mockUseResolveGate.mockReset();
  mockUseGates.mockReturnValue({ data: [] });
  mockUseResolveGate.mockReturnValue({ mutate: vi.fn() });
});

describe("TaskDetailPane — header, description, actions", () => {
  it("renders title, status badge, and metadata badges without crashing", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    render(<TaskDetailPane {...noopProps()} />);
    expect(screen.getByText("Fix the thing")).toBeInTheDocument();
    expect(screen.getByText("t1")).toBeInTheDocument();
    expect(screen.getByText("demo")).toBeInTheDocument();
    expect(screen.getByText("P2")).toBeInTheDocument();
  });

  it("shows Loading… title while isLoading with no cached task", () => {
    mockUseTask.mockReturnValue({ data: undefined, isLoading: true, isError: false });
    render(<TaskDetailPane {...noopProps()} />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("renders the description block only when non-empty", () => {
    mockUseTask.mockReturnValue({
      data: { ...fixtureTask, description: "Some details here" },
      isLoading: false,
      isError: false,
    });
    render(<TaskDetailPane {...noopProps()} />);
    expect(screen.getByText("Some details here")).toBeInTheDocument();
  });

  it("shows the Approve action for an AWAITING_APPROVAL task", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    render(<TaskDetailPane {...noopProps()} />);
    expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/task-detail/__tests__/index.test.tsx`
Expected: FAIL — `Failed to resolve import "../index"`.

- [ ] **Step 3: Write minimal implementation**

`dashboard/src/panes/task-detail/index.tsx`:

```tsx
import { useTask, useGates, type Task } from "../../api/hooks";
import StatusBadge from "../../components/StatusBadge";
import TaskActions from "../../components/TaskActions";
import type { PaneViewProps } from "../types";
import type { TaskDetailArgs } from "./manifest";

type TaskWithLooseFields = Task & {
  intelligence_class?: string;
  branch_name?: string;
};

export default function TaskDetailPane({ args }: PaneViewProps<TaskDetailArgs>) {
  const { data: task, isLoading } = useTask(args.taskId);
  useGates({ projectId: task?.project_id });

  const loose = task as TaskWithLooseFields | undefined;

  return (
    <div className="flex flex-col gap-4 p-4">
      <header className="min-w-0">
        <p className="truncate font-mono text-xs text-gray-500">{args.taskId}</p>
        <h2 className="mt-0.5 truncate text-lg font-semibold text-gray-100">
          {isLoading && !task ? "Loading…" : (task?.title ?? "Loading…")}
        </h2>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          {task?.status && <StatusBadge status={task.status} />}
          {task?.project_id && <span className="text-gray-400">{task.project_id}</span>}
          {task?.priority != null && (
            <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
              P{task.priority}
            </span>
          )}
          {task?.task_type && (
            <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
              {task.task_type}
            </span>
          )}
          {task?.profile_id && (
            <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
              {task.profile_id}
            </span>
          )}
          {loose?.intelligence_class && (
            <span className="rounded bg-indigo-500/10 px-2 py-0.5 text-indigo-300">
              {loose.intelligence_class}
            </span>
          )}
          {task?.is_plan_subtask && (
            <span className="rounded bg-indigo-500/10 px-2 py-0.5 text-indigo-400">
              subtask
            </span>
          )}
        </div>
      </header>

      {task && <TaskActions task={task} />}

      {task?.description ? (
        <section>
          <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">Description</h3>
          <div className="whitespace-pre-wrap rounded-lg border border-gray-800 bg-gray-900 p-3 text-sm text-gray-300">
            {task.description}
          </div>
        </section>
      ) : null}
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/task-detail/__tests__/index.test.tsx`
Expected: PASS — 4 tests.

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/shell/useShellPane.ts dashboard/src/panes/task-detail/index.tsx \
  dashboard/src/panes/task-detail/__tests__/index.test.tsx
git commit -m "feat(dashboard): task-detail pane — header, description, actions bar"
```

---

## Task 4: Component — metadata grid, PR link, subtasks/deps/blocks sections

**Files:**
- Modify: `dashboard/src/panes/task-detail/index.tsx`
- Modify: `dashboard/src/panes/task-detail/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `TaskRef` type re-exported alongside `Task` from `dashboard/src/api/hooks.ts` (already imported in `TaskSidebar.tsx` as `type TaskRef`); `useShellPane().open` from `dashboard/src/shell/useShellPane.ts` (Task 3).
- Produces: metadata grid, PR section, and `Subtasks`/`Depends on`/`Blocks` sections rendered inside `TaskDetailPane`; a local, unexported `formatDate(value?: string | number | null): string` helper reused by later sections if needed.

- [ ] **Step 1: Write the failing test**

Append to `dashboard/src/panes/task-detail/__tests__/index.test.tsx`:

```tsx
describe("TaskDetailPane — metadata, PR link, relationships", () => {
  it("renders the metadata grid fields", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    render(<TaskDetailPane {...noopProps()} />);
    expect(screen.getByText("agent-1")).toBeInTheDocument();
    expect(screen.getByText("0 / 3")).toBeInTheDocument();
  });

  it("renders the PR link only when pr_url is set", () => {
    mockUseTask.mockReturnValue({
      data: { ...fixtureTask, pr_url: "https://github.com/org/repo/pull/1" },
      isLoading: false,
      isError: false,
    });
    render(<TaskDetailPane {...noopProps()} />);
    expect(screen.getByRole("link", { name: /pull\/1/i })).toHaveAttribute(
      "href",
      "https://github.com/org/repo/pull/1",
    );
  });

  it("does not render a Pull request section when pr_url is unset", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    render(<TaskDetailPane {...noopProps()} />);
    expect(screen.queryByText("Pull request")).not.toBeInTheDocument();
  });

  it("clicking a subtask row calls useShellPane().open with the ref's taskId", async () => {
    const openMock = vi.fn();
    vi.doMock("../../../shell/useShellPane", () => ({
      useShellPane: () => ({ open: openMock, close: vi.fn(), state: { kind: "closed" } }),
    }));
    const { default: FreshPane } = await import("../index");
    mockUseTask.mockReturnValue({
      data: { ...fixtureTask, subtasks: [{ id: "t2", title: "Sub one", status: "COMPLETED" }] },
      isLoading: false,
      isError: false,
    });
    render(<FreshPane {...noopProps()} />);
    screen.getByText("Sub one").click();
    expect(openMock).toHaveBeenCalledWith("task-detail", { taskId: "t2" });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/task-detail/__tests__/index.test.tsx`
Expected: FAIL — metadata grid / PR link / subtask row text not found (not rendered yet).

- [ ] **Step 3: Extend the implementation**

Replace the body of `dashboard/src/panes/task-detail/index.tsx` (everything after the `{task?.description ...}` block, before the closing `</div>`) — full file:

```tsx
import { ArrowTopRightOnSquareIcon } from "@heroicons/react/24/outline";
import { useTask, useGates, type Task, type TaskRef } from "../../api/hooks";
import StatusBadge from "../../components/StatusBadge";
import TaskActions from "../../components/TaskActions";
import { useShellPane } from "../../shell/useShellPane";
import type { PaneViewProps } from "../types";
import type { TaskDetailArgs } from "./manifest";

type TaskWithLooseFields = Task & {
  intelligence_class?: string;
  branch_name?: string;
};

export default function TaskDetailPane({ args }: PaneViewProps<TaskDetailArgs>) {
  const { data: task, isLoading } = useTask(args.taskId);
  useGates({ projectId: task?.project_id });
  const { open } = useShellPane();

  const loose = task as TaskWithLooseFields | undefined;

  return (
    <div className="flex flex-col gap-4 p-4">
      <header className="min-w-0">
        <p className="truncate font-mono text-xs text-gray-500">{args.taskId}</p>
        <h2 className="mt-0.5 truncate text-lg font-semibold text-gray-100">
          {isLoading && !task ? "Loading…" : (task?.title ?? "Loading…")}
        </h2>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          {task?.status && <StatusBadge status={task.status} />}
          {task?.project_id && <span className="text-gray-400">{task.project_id}</span>}
          {task?.priority != null && (
            <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
              P{task.priority}
            </span>
          )}
          {task?.task_type && (
            <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
              {task.task_type}
            </span>
          )}
          {task?.profile_id && (
            <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
              {task.profile_id}
            </span>
          )}
          {loose?.intelligence_class && (
            <span className="rounded bg-indigo-500/10 px-2 py-0.5 text-indigo-300">
              {loose.intelligence_class}
            </span>
          )}
          {task?.is_plan_subtask && (
            <span className="rounded bg-indigo-500/10 px-2 py-0.5 text-indigo-400">
              subtask
            </span>
          )}
        </div>
      </header>

      {task && <TaskActions task={task} />}

      {task?.description ? (
        <section>
          <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">Description</h3>
          <div className="whitespace-pre-wrap rounded-lg border border-gray-800 bg-gray-900 p-3 text-sm text-gray-300">
            {task.description}
          </div>
        </section>
      ) : null}

      <section>
        <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">Details</h3>
        <div className="grid grid-cols-1 gap-x-4 gap-y-2 rounded-lg border border-gray-800 bg-gray-900 p-3 text-sm sm:grid-cols-2">
          <MetaField label="Agent" value={task?.assigned_agent ?? "—"} />
          <MetaField
            label="Retries"
            value={`${task?.retry_count ?? 0} / ${task?.max_retries ?? 3}`}
          />
          <MetaField label="Requires approval" value={task?.requires_approval ? "Yes" : "No"} />
          <MetaField
            label="Auto-approve plan"
            value={task?.auto_approve_plan ? "Yes" : "No"}
          />
          <MetaField
            label="Skip verification"
            value={task?.skip_verification ? "Yes" : "No"}
          />
          <MetaField label="Branch" value={loose?.branch_name ?? "—"} mono />
          <MetaField label="Created" value={formatDate(task?.created_at)} />
          <MetaField label="Updated" value={formatDate(task?.updated_at)} />
          {task?.parent_task_id && (
            <div>
              <span className="text-xs text-gray-500">Parent task</span>
              <p className="mt-0.5">
                <button
                  type="button"
                  onClick={() => open("task-detail", { taskId: task.parent_task_id })}
                  className="font-mono text-xs text-indigo-400 hover:underline"
                >
                  {task.parent_task_id}
                </button>
              </p>
            </div>
          )}
        </div>
      </section>

      {task?.pr_url && (
        <section>
          <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">Pull request</h3>
          <a
            href={task.pr_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 text-sm text-indigo-400 hover:underline"
          >
            {task.pr_url} <ArrowTopRightOnSquareIcon className="h-3.5 w-3.5" />
          </a>
        </section>
      )}

      {(task?.subtasks ?? []).length > 0 && (
        <TaskRefSection title="Subtasks" items={task!.subtasks!} onOpen={open} />
      )}
      {(task?.depends_on ?? []).length > 0 && (
        <TaskRefSection title="Depends on" items={task!.depends_on!} onOpen={open} />
      )}
      {(task?.blocks ?? []).length > 0 && (
        <TaskRefSection title="Blocks" items={task!.blocks!} onOpen={open} />
      )}
    </div>
  );
}

function MetaField({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0">
      <span className="text-xs text-gray-500">{label}</span>
      <p className={`truncate text-gray-300 ${mono ? "font-mono text-xs" : ""}`} title={value}>
        {value}
      </p>
    </div>
  );
}

function TaskRefSection({
  title,
  items,
  onOpen,
}: {
  title: string;
  items: TaskRef[];
  onOpen: (viewId: string, args: unknown) => void;
}) {
  return (
    <section>
      <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">{title}</h3>
      <ul className="space-y-1">
        {items.map((r) => (
          <li
            key={r.id}
            className="flex items-center justify-between gap-2 rounded-lg border border-gray-800 bg-gray-900 px-3 py-2 text-sm"
          >
            <button
              type="button"
              onClick={() => onOpen("task-detail", { taskId: r.id })}
              className="min-w-0 flex-1 truncate text-left text-indigo-400 hover:underline"
              title={r.title}
            >
              {r.title}
            </button>
            <StatusBadge status={r.status} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function formatDate(value?: string | number | null): string {
  if (value == null) return "—";
  try {
    const d = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    return d.toLocaleString();
  } catch {
    return String(value);
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/task-detail/__tests__/index.test.tsx`
Expected: PASS — 8 tests.

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/panes/task-detail/index.tsx dashboard/src/panes/task-detail/__tests__/index.test.tsx
git commit -m "feat(dashboard): task-detail pane — metadata grid, PR link, relationship sections"
```

---

## Task 5: Component — gates section + registry wiring

**Files:**
- Modify: `dashboard/src/api/hooks.ts` (add optional `enabled` to `useGates`)
- Modify: `dashboard/src/panes/task-detail/index.tsx`
- Modify: `dashboard/src/panes/task-detail/__tests__/index.test.tsx`
- Create: `dashboard/src/panes/registry.ts`
- Create: `dashboard/src/panes/__tests__/registry.test.ts`

**Interfaces:**
- Consumes: `manifest`, default export `TaskDetailPane` from `dashboard/src/panes/task-detail/` (Tasks 1, 3, 4); `GateSummary` type from `dashboard/src/api/hooks.ts:1043`; `useResolveGate` from `dashboard/src/api/hooks.ts:1081`.
- Produces: `PANE_REGISTRY: Record<string, PaneEntry>` exported from `dashboard/src/panes/registry.ts`, containing the `"task-detail"` entry — consumed by Task 10's parity test and (in a later, out-of-scope phase) the shell.

- [ ] **Step 1: Write the failing test — gates**

Append to `dashboard/src/panes/task-detail/__tests__/index.test.tsx`:

```tsx
describe("TaskDetailPane — gates", () => {
  it("shows only gates whose task_ids include this task", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    mockUseGates.mockReturnValue({
      data: [
        { id: "g1", gate_type: "human", status: "open", task_ids: ["t1"], project_id: "demo", title: "g1" },
        { id: "g2", gate_type: "human", status: "open", task_ids: ["other"], project_id: "demo", title: "g2" },
      ],
    });
    render(<TaskDetailPane {...noopProps()} />);
    expect(screen.getByText(/human/)).toBeInTheDocument();
    expect(screen.getAllByText(/human/)).toHaveLength(1);
  });

  it("Approve calls useResolveGate().mutate with the gate id", () => {
    const mutate = vi.fn();
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    mockUseGates.mockReturnValue({
      data: [
        { id: "g1", gate_type: "human", status: "open", task_ids: ["t1"], project_id: "demo", title: "g1" },
      ],
    });
    mockUseResolveGate.mockReturnValue({ mutate });
    render(<TaskDetailPane {...noopProps()} />);
    screen.getByRole("button", { name: /approve/i, hidden: false }).click();
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ gate_id: "g1", resolution: "approve" }),
    );
  });

  it("omits the Gates section entirely when there are no matching gates", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    mockUseGates.mockReturnValue({ data: [] });
    render(<TaskDetailPane {...noopProps()} />);
    expect(screen.queryByText("Gates")).not.toBeInTheDocument();
  });
});
```

Note: this reuses the same `useResolveGate` mock as the `TaskActions` "Approve" button test in Task 3 (both render text "Approve") — this test disambiguates by scoping to gate rows via `hidden: false` is not sufficient on its own; the actual disambiguation happens because the gates test only registers one gate row's Approve button when `TaskActions`'s own Approve button is absent (fixture task stays `AWAITING_APPROVAL`, which *does* render its own "Approve" — to keep this test unambiguous, change the fixture task status for these three gates tests to `IN_PROGRESS`, which shows no `TaskActions` "Approve" button):

Replace `mockUseTask.mockReturnValue({ data: fixtureTask, ...})` in all three gates tests above with `mockUseTask.mockReturnValue({ data: { ...fixtureTask, status: "IN_PROGRESS" }, isLoading: false, isError: false })`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/task-detail/__tests__/index.test.tsx`
Expected: FAIL — no "Gates" section rendered, `useResolveGate` mutate never called.

- [ ] **Step 3: Add `enabled` support to `useGates`**

In `dashboard/src/api/hooks.ts`, replace:

```ts
export function useGates(
  opts: { projectId?: string; status?: string; gateType?: string } = {},
) {
  return useQuery({
    queryKey: [
      "gates",
      opts.projectId ?? "all",
      opts.status ?? "any",
      opts.gateType ?? "any",
    ],
    queryFn: async () => {
      const body: Record<string, unknown> = {};
      if (opts.projectId) body.project_id = opts.projectId;
      if (opts.status) body.status = opts.status;
      if (opts.gateType) body.gate_type = opts.gateType;
      const { data } = await gateList({ body, throwOnError: true });
      return ((data as GateListResponse).gates ?? []) as GateSummary[];
    },
    refetchInterval: 20_000,
  });
}
```

with:

```ts
export function useGates(
  opts: { projectId?: string; status?: string; gateType?: string; enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: [
      "gates",
      opts.projectId ?? "all",
      opts.status ?? "any",
      opts.gateType ?? "any",
    ],
    queryFn: async () => {
      const body: Record<string, unknown> = {};
      if (opts.projectId) body.project_id = opts.projectId;
      if (opts.status) body.status = opts.status;
      if (opts.gateType) body.gate_type = opts.gateType;
      const { data } = await gateList({ body, throwOnError: true });
      return ((data as GateListResponse).gates ?? []) as GateSummary[];
    },
    refetchInterval: 20_000,
    enabled: opts.enabled ?? true,
  });
}
```

- [ ] **Step 4: Add the gates section to `TaskDetailPane`**

In `dashboard/src/panes/task-detail/index.tsx`:

Add to the imports:

```ts
import { useTask, useGates, useResolveGate, type Task, type TaskRef, type GateSummary } from "../../api/hooks";
```

(remove the old `useTask, useGates, type Task, type TaskRef` import line and replace with the one above).

Replace:

```ts
  const { data: task, isLoading } = useTask(args.taskId);
  useGates({ projectId: task?.project_id });
  const { open } = useShellPane();
```

with:

```ts
  const { data: task, isLoading } = useTask(args.taskId);
  const { data: gates } = useGates({ projectId: task?.project_id, enabled: !!task?.project_id });
  const resolveGate = useResolveGate();
  const { open } = useShellPane();

  const taskGates = (
    (gates ?? []) as Array<GateSummary & { task_ids?: string[] }>
  ).filter((g) => (g.task_ids ?? []).includes(args.taskId));
```

Insert the Gates section immediately after the `{task?.pr_url && (...)}` block and before the `Subtasks` section:

```tsx
      {taskGates.length > 0 && (
        <section>
          <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">Gates</h3>
          <ul className="space-y-1.5">
            {taskGates.map((g) => (
              <li
                key={g.id}
                className="flex items-center justify-between rounded-lg border border-gray-800 bg-gray-900 p-2.5 text-sm"
              >
                <span className="text-gray-300">
                  {g.gate_type} <span className="text-xs text-gray-500">{g.status}</span>
                </span>
                {g.gate_type === "human" && g.status === "open" && (
                  <span className="flex gap-1.5">
                    <button
                      onClick={() =>
                        resolveGate.mutate({ gate_id: g.id, resolved_by: "dashboard", resolution: "approve" })
                      }
                      className="rounded bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-500"
                    >
                      Approve
                    </button>
                    <button
                      onClick={() =>
                        resolveGate.mutate({ gate_id: g.id, resolved_by: "dashboard", resolution: "reject" })
                      }
                      className="rounded bg-red-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-red-500"
                    >
                      Reject
                    </button>
                  </span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/task-detail/__tests__/index.test.tsx`
Expected: PASS — 11 tests.

- [ ] **Step 6: Write the failing registry test**

`dashboard/src/panes/__tests__/registry.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { PANE_REGISTRY } from "../registry";

describe("PANE_REGISTRY", () => {
  it("resolves the task-detail view", () => {
    expect(PANE_REGISTRY["task-detail"]).toBeDefined();
    expect(PANE_REGISTRY["task-detail"].manifest.id).toBe("task-detail");
    expect(typeof PANE_REGISTRY["task-detail"].Component).toBe("function");
  });

  it("every entry's manifest.id matches its registry key", () => {
    for (const [key, entry] of Object.entries(PANE_REGISTRY)) {
      expect(entry.manifest.id).toBe(key);
    }
  });

  it("has no duplicate open_shortcut values", () => {
    const shortcuts = Object.values(PANE_REGISTRY)
      .map((e) => e.manifest.open_shortcut)
      .filter((s): s is string => !!s);
    expect(new Set(shortcuts).size).toBe(shortcuts.length);
  });
});
```

- [ ] **Step 7: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/__tests__/registry.test.ts`
Expected: FAIL — `Failed to resolve import "../registry"`.

- [ ] **Step 8: Write `dashboard/src/panes/registry.ts`**

```ts
import type { PaneEntry } from "./types";
import { manifest as taskDetailManifest } from "./task-detail/manifest";
import TaskDetailPane from "./task-detail";

export const PANE_REGISTRY: Record<string, PaneEntry> = {
  "task-detail": { manifest: taskDetailManifest, Component: TaskDetailPane },
};

for (const [key, entry] of Object.entries(PANE_REGISTRY)) {
  if (entry.manifest.id !== key) {
    throw new Error(
      `Pane registry key "${key}" does not match manifest.id "${entry.manifest.id}"`,
    );
  }
}

const seenShortcuts = new Set<string>();
for (const entry of Object.values(PANE_REGISTRY)) {
  const shortcut = entry.manifest.open_shortcut;
  if (!shortcut) continue;
  if (seenShortcuts.has(shortcut)) {
    throw new Error(`Duplicate pane open_shortcut "${shortcut}"`);
  }
  seenShortcuts.add(shortcut);
}
```

- [ ] **Step 9: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/__tests__/registry.test.ts`
Expected: PASS — 3 tests.

- [ ] **Step 10: Run the full frontend test suite to confirm no regressions**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run`
Expected: PASS — all suites green.

- [ ] **Step 11: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/api/hooks.ts dashboard/src/panes/task-detail/index.tsx \
  dashboard/src/panes/task-detail/__tests__/index.test.tsx \
  dashboard/src/panes/registry.ts dashboard/src/panes/__tests__/registry.test.ts
git commit -m "feat(dashboard): task-detail pane — gates section, useGates(enabled), pane registry"
```

---

## Task 6: Toolbar + pane-scoped shortcuts (`o`, `c`, `r`, `.`)

**Files:**
- Modify: `dashboard/src/panes/task-detail/index.tsx`
- Modify: `dashboard/src/panes/task-detail/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `setToolbar`, `setShortcuts` from `PaneViewProps` (Task 1); `useNavigate` from `react-router-dom`; `ClipboardIcon`, `ArrowTopRightOnSquareIcon` from `@heroicons/react/24/outline`.
- Produces: two toolbar actions (`open-full`, `copy-id`) and four shortcut bindings (`o`, `c`, `r`, `.`) registered on every render via `setToolbar`/`setShortcuts`.

Per task-detail spec §6 and §12: `c` maps to the existing Delete flow (no distinct "close" concept exists in `TaskActions` today — this is a known, spec-flagged gap, not introduced by this task) by opening `TaskActions`'s delete confirmation. Since `TaskActions` owns its own delete modal internally and doesn't expose an external "open delete modal" hook, `c` fires the same `useDeleteTask()` mutation directly with an inline confirm, rather than reaching into `TaskActions`'s internal state — this view manages its own three modals (`close`, `reopen`, `more`) alongside `TaskActions`'s independent action bar, matching the spec's explicit call-out that duplicating `TaskActions`'s visibility logic locally is accepted for v1 (task-detail spec §6, §12).

- [ ] **Step 1: Write the failing test**

Append to `dashboard/src/panes/task-detail/__tests__/index.test.tsx`:

```tsx
import { MemoryRouter } from "react-router-dom";

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

function renderWithRouter(ui: React.ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

describe("TaskDetailPane — toolbar and shortcuts", () => {
  it("registers Open full detail page and Copy id toolbar actions", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const setToolbar = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} setToolbar={setToolbar} />);
    const lastCall = setToolbar.mock.calls.at(-1)?.[0];
    expect(lastCall.map((a: { id: string }) => a.id)).toEqual(["open-full", "copy-id"]);
  });

  it("Open full detail page navigates to /tasks/:id", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const setToolbar = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} setToolbar={setToolbar} />);
    const actions = setToolbar.mock.calls.at(-1)?.[0];
    actions.find((a: { id: string }) => a.id === "open-full").onClick();
    expect(mockNavigate).toHaveBeenCalledWith("/tasks/t1");
  });

  it("Copy id writes the task id to the clipboard", async () => {
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const setToolbar = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} setToolbar={setToolbar} />);
    const actions = setToolbar.mock.calls.at(-1)?.[0];
    actions.find((a: { id: string }) => a.id === "copy-id").onClick();
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("t1");
  });

  it("registers exactly the o/c/r/. shortcut keys", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const setShortcuts = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} setShortcuts={setShortcuts} />);
    const lastCall = setShortcuts.mock.calls.at(-1)?.[0];
    expect(lastCall.map((b: { key: string }) => b.key)).toEqual(["o", "c", "r", "."]);
  });

  it("o shortcut navigates to the full detail page", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const setShortcuts = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} setShortcuts={setShortcuts} />);
    const bindings = setShortcuts.mock.calls.at(-1)?.[0];
    bindings.find((b: { key: string }) => b.key === "o").onFire();
    expect(mockNavigate).toHaveBeenCalledWith("/tasks/t1");
  });

  it("c shortcut opens the close/delete confirmation", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const setShortcuts = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} setShortcuts={setShortcuts} />);
    const bindings = setShortcuts.mock.calls.at(-1)?.[0];
    bindings.find((b: { key: string }) => b.key === "c").onFire();
    expect(screen.getByText(/delete/i)).toBeInTheDocument();
  });

  it(". shortcut opens the more-actions dropdown", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const setShortcuts = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} setShortcuts={setShortcuts} />);
    const bindings = setShortcuts.mock.calls.at(-1)?.[0];
    bindings.find((b: { key: string }) => b.key === ".").onFire();
    expect(screen.getByRole("menu")).toBeInTheDocument();
  });
});
```

Also wrap every earlier `render(<TaskDetailPane ...>)` call in this file with `renderWithRouter(...)` (find-and-replace `render(<TaskDetailPane` → `renderWithRouter(<TaskDetailPane` across the file) since `useNavigate` now requires a router context for all tests, not just this section's.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/task-detail/__tests__/index.test.tsx`
Expected: FAIL — `setToolbar`/`setShortcuts` never called; no menu/dialog rendered.

- [ ] **Step 3: Implement toolbar, shortcuts, and the two local modals**

Full replacement of `dashboard/src/panes/task-detail/index.tsx`:

```tsx
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  ArrowTopRightOnSquareIcon,
  ClipboardIcon,
} from "@heroicons/react/24/outline";
import {
  useTask,
  useGates,
  useResolveGate,
  useDeleteTask,
  useReopenWithFeedback,
  type Task,
  type TaskRef,
  type GateSummary,
} from "../../api/hooks";
import StatusBadge from "../../components/StatusBadge";
import TaskActions from "../../components/TaskActions";
import Modal from "../../components/Modal";
import { useShellPane } from "../../shell/useShellPane";
import type { PaneViewProps } from "../types";
import type { TaskDetailArgs } from "./manifest";

type TaskWithLooseFields = Task & {
  intelligence_class?: string;
  branch_name?: string;
};

type LocalModal = "close" | "reopen" | null;

export default function TaskDetailPane({
  args,
  setToolbar,
  setShortcuts,
}: PaneViewProps<TaskDetailArgs>) {
  const navigate = useNavigate();
  const { data: task, isLoading } = useTask(args.taskId);
  const { data: gates } = useGates({ projectId: task?.project_id, enabled: !!task?.project_id });
  const resolveGate = useResolveGate();
  const deleteTask = useDeleteTask();
  const reopenWithFeedback = useReopenWithFeedback();
  const { open } = useShellPane();

  const [modal, setModal] = useState<LocalModal>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const [feedback, setFeedback] = useState("");

  const loose = task as TaskWithLooseFields | undefined;

  const taskGates = (
    (gates ?? []) as Array<GateSummary & { task_ids?: string[] }>
  ).filter((g) => (g.task_ids ?? []).includes(args.taskId));

  setToolbar([
    {
      id: "open-full",
      label: "Open full detail page",
      icon: ArrowTopRightOnSquareIcon,
      onClick: () => navigate(`/tasks/${args.taskId}`),
    },
    {
      id: "copy-id",
      label: "Copy id",
      icon: ClipboardIcon,
      onClick: () => navigator.clipboard.writeText(args.taskId),
    },
  ]);

  setShortcuts([
    { key: "o", label: "Open full detail page", onFire: () => navigate(`/tasks/${args.taskId}`) },
    { key: "c", label: "Close task", onFire: () => setModal("close") },
    { key: "r", label: "Reopen with feedback", onFire: () => setModal("reopen") },
    { key: ".", label: "More actions", onFire: () => setMoreOpen(true) },
  ]);

  return (
    <div className="flex flex-col gap-4 p-4">
      <header className="min-w-0">
        <p className="truncate font-mono text-xs text-gray-500">{args.taskId}</p>
        <h2 className="mt-0.5 truncate text-lg font-semibold text-gray-100">
          {isLoading && !task ? "Loading…" : (task?.title ?? "Loading…")}
        </h2>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          {task?.status && <StatusBadge status={task.status} />}
          {task?.project_id && <span className="text-gray-400">{task.project_id}</span>}
          {task?.priority != null && (
            <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
              P{task.priority}
            </span>
          )}
          {task?.task_type && (
            <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
              {task.task_type}
            </span>
          )}
          {task?.profile_id && (
            <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
              {task.profile_id}
            </span>
          )}
          {loose?.intelligence_class && (
            <span className="rounded bg-indigo-500/10 px-2 py-0.5 text-indigo-300">
              {loose.intelligence_class}
            </span>
          )}
          {task?.is_plan_subtask && (
            <span className="rounded bg-indigo-500/10 px-2 py-0.5 text-indigo-400">
              subtask
            </span>
          )}
        </div>
      </header>

      {task && <TaskActions task={task} />}

      {task?.description ? (
        <section>
          <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">Description</h3>
          <div className="whitespace-pre-wrap rounded-lg border border-gray-800 bg-gray-900 p-3 text-sm text-gray-300">
            {task.description}
          </div>
        </section>
      ) : null}

      <section>
        <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">Details</h3>
        <div className="grid grid-cols-1 gap-x-4 gap-y-2 rounded-lg border border-gray-800 bg-gray-900 p-3 text-sm sm:grid-cols-2">
          <MetaField label="Agent" value={task?.assigned_agent ?? "—"} />
          <MetaField
            label="Retries"
            value={`${task?.retry_count ?? 0} / ${task?.max_retries ?? 3}`}
          />
          <MetaField label="Requires approval" value={task?.requires_approval ? "Yes" : "No"} />
          <MetaField
            label="Auto-approve plan"
            value={task?.auto_approve_plan ? "Yes" : "No"}
          />
          <MetaField
            label="Skip verification"
            value={task?.skip_verification ? "Yes" : "No"}
          />
          <MetaField label="Branch" value={loose?.branch_name ?? "—"} mono />
          <MetaField label="Created" value={formatDate(task?.created_at)} />
          <MetaField label="Updated" value={formatDate(task?.updated_at)} />
          {task?.parent_task_id && (
            <div>
              <span className="text-xs text-gray-500">Parent task</span>
              <p className="mt-0.5">
                <button
                  type="button"
                  onClick={() => open("task-detail", { taskId: task.parent_task_id })}
                  className="font-mono text-xs text-indigo-400 hover:underline"
                >
                  {task.parent_task_id}
                </button>
              </p>
            </div>
          )}
        </div>
      </section>

      {task?.pr_url && (
        <section>
          <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">Pull request</h3>
          <a
            href={task.pr_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 text-sm text-indigo-400 hover:underline"
          >
            {task.pr_url} <ArrowTopRightOnSquareIcon className="h-3.5 w-3.5" />
          </a>
        </section>
      )}

      {taskGates.length > 0 && (
        <section>
          <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">Gates</h3>
          <ul className="space-y-1.5">
            {taskGates.map((g) => (
              <li
                key={g.id}
                className="flex items-center justify-between rounded-lg border border-gray-800 bg-gray-900 p-2.5 text-sm"
              >
                <span className="text-gray-300">
                  {g.gate_type} <span className="text-xs text-gray-500">{g.status}</span>
                </span>
                {g.gate_type === "human" && g.status === "open" && (
                  <span className="flex gap-1.5">
                    <button
                      onClick={() =>
                        resolveGate.mutate({ gate_id: g.id, resolved_by: "dashboard", resolution: "approve" })
                      }
                      className="rounded bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-500"
                    >
                      Approve
                    </button>
                    <button
                      onClick={() =>
                        resolveGate.mutate({ gate_id: g.id, resolved_by: "dashboard", resolution: "reject" })
                      }
                      className="rounded bg-red-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-red-500"
                    >
                      Reject
                    </button>
                  </span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {(task?.subtasks ?? []).length > 0 && (
        <TaskRefSection title="Subtasks" items={task!.subtasks!} onOpen={open} />
      )}
      {(task?.depends_on ?? []).length > 0 && (
        <TaskRefSection title="Depends on" items={task!.depends_on!} onOpen={open} />
      )}
      {(task?.blocks ?? []).length > 0 && (
        <TaskRefSection title="Blocks" items={task!.blocks!} onOpen={open} />
      )}

      <Modal open={modal === "close"} onClose={() => setModal(null)} title="Delete Task">
        <div className="space-y-4">
          <p className="text-sm text-gray-300">
            Are you sure you want to delete <strong>{task?.title}</strong>? This cannot be undone.
          </p>
          <div className="flex justify-end gap-2">
            <button
              onClick={() => setModal(null)}
              className="rounded-md border border-gray-600 bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
            >
              Cancel
            </button>
            <button
              onClick={() =>
                deleteTask.mutate({ task_id: args.taskId }, { onSuccess: () => setModal(null) })
              }
              disabled={deleteTask.isPending}
              className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
            >
              {deleteTask.isPending ? "Deleting..." : "Delete"}
            </button>
          </div>
        </div>
      </Modal>

      <Modal open={modal === "reopen"} onClose={() => setModal(null)} title="Reopen with Feedback">
        <div className="space-y-4">
          <textarea
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            placeholder="Provide feedback..."
            rows={4}
            className="w-full rounded-md border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-200 placeholder-gray-500 focus:border-indigo-500 focus:outline-none"
            autoFocus
          />
          <div className="flex justify-end gap-2">
            <button
              onClick={() => setModal(null)}
              className="rounded-md border border-gray-600 bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
            >
              Cancel
            </button>
            <button
              onClick={() =>
                reopenWithFeedback.mutate(
                  { task_id: args.taskId, feedback },
                  { onSuccess: () => setModal(null) },
                )
              }
              disabled={!feedback.trim() || reopenWithFeedback.isPending}
              className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              {reopenWithFeedback.isPending ? "Submitting..." : "Submit"}
            </button>
          </div>
        </div>
      </Modal>

      {moreOpen && (
        <div
          role="menu"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
          onClick={() => setMoreOpen(false)}
        >
          <ul
            className="min-w-[220px] rounded-lg border border-gray-800 bg-gray-900 p-1.5 text-sm"
            onClick={(e) => e.stopPropagation()}
          >
            <li>
              <button
                role="menuitem"
                onClick={() => {
                  navigate(`/tasks/${args.taskId}`);
                  setMoreOpen(false);
                }}
                className="block w-full rounded px-3 py-1.5 text-left text-gray-200 hover:bg-gray-800"
              >
                Open full detail page
              </button>
            </li>
            <li>
              <button
                role="menuitem"
                onClick={() => {
                  setModal("reopen");
                  setMoreOpen(false);
                }}
                className="block w-full rounded px-3 py-1.5 text-left text-gray-200 hover:bg-gray-800"
              >
                Reopen with feedback
              </button>
            </li>
            <li>
              <button
                role="menuitem"
                onClick={() => {
                  setModal("close");
                  setMoreOpen(false);
                }}
                className="block w-full rounded px-3 py-1.5 text-left text-red-400 hover:bg-gray-800"
              >
                Delete
              </button>
            </li>
          </ul>
        </div>
      )}
    </div>
  );
}

function MetaField({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0">
      <span className="text-xs text-gray-500">{label}</span>
      <p className={`truncate text-gray-300 ${mono ? "font-mono text-xs" : ""}`} title={value}>
        {value}
      </p>
    </div>
  );
}

function TaskRefSection({
  title,
  items,
  onOpen,
}: {
  title: string;
  items: TaskRef[];
  onOpen: (viewId: string, args: unknown) => void;
}) {
  return (
    <section>
      <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">{title}</h3>
      <ul className="space-y-1">
        {items.map((r) => (
          <li
            key={r.id}
            className="flex items-center justify-between gap-2 rounded-lg border border-gray-800 bg-gray-900 px-3 py-2 text-sm"
          >
            <button
              type="button"
              onClick={() => onOpen("task-detail", { taskId: r.id })}
              className="min-w-0 flex-1 truncate text-left text-indigo-400 hover:underline"
              title={r.title}
            >
              {r.title}
            </button>
            <StatusBadge status={r.status} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function formatDate(value?: string | number | null): string {
  if (value == null) return "—";
  try {
    const d = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    return d.toLocaleString();
  } catch {
    return String(value);
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/task-detail/__tests__/index.test.tsx`
Expected: PASS — 18 tests.

- [ ] **Step 5: Run the full frontend suite**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run`
Expected: PASS — all suites green.

- [ ] **Step 6: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/panes/task-detail/index.tsx dashboard/src/panes/task-detail/__tests__/index.test.tsx
git commit -m "feat(dashboard): task-detail pane — toolbar + o/c/r/. shortcuts, close/reopen modals"
```

---

## Task 7: Not-found and loading edge-case coverage

**Files:**
- Modify: `dashboard/src/panes/task-detail/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: nothing new — exercises the existing `isError`/`isLoading` branches from Task 3's implementation.
- Produces: nothing new — this task closes the remaining gap between task-detail spec §8 and the test suite (not-found rendering, close-affordance via `[Open full detail page]` still present when not found).

- [ ] **Step 1: Write the failing test**

Append to `dashboard/src/panes/task-detail/__tests__/index.test.tsx`:

```tsx
describe("TaskDetailPane — not found and loading", () => {
  it("renders Task not found on isError, keeping Open full detail page usable", () => {
    mockUseTask.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    const setToolbar = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} setToolbar={setToolbar} />);
    expect(screen.getByText("Task not found.")).toBeInTheDocument();
    const actions = setToolbar.mock.calls.at(-1)?.[0];
    expect(actions.find((a: { id: string }) => a.id === "open-full")).toBeDefined();
  });

  it("renders Loading… without crashing when isLoading with no cached data", () => {
    mockUseTask.mockReturnValue({ data: undefined, isLoading: true, isError: false });
    expect(() => renderWithRouter(<TaskDetailPane {...noopProps()} />)).not.toThrow();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/task-detail/__tests__/index.test.tsx`
Expected: FAIL — `Task not found.` text not rendered (current implementation doesn't branch on `isError`).

- [ ] **Step 3: Add the not-found branch**

In `dashboard/src/panes/task-detail/index.tsx`, change the function signature to also destructure `isError`:

```ts
  const { data: task, isLoading, isError } = useTask(args.taskId);
```

Wrap the body's `<div className="flex flex-col gap-4 p-4">...</div>` return in a not-found branch — insert immediately before that `return (`:

```tsx
  if (isError) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 p-8 text-center">
        <p className="text-sm text-gray-400">Task not found.</p>
      </div>
    );
  }

```

Note: `setToolbar`/`setShortcuts` must still be called before this early return so the toolbar's `[Open full detail page]` action stays registered even in the not-found state (task-detail spec §8) — keep the `setToolbar([...])` / `setShortcuts([...])` calls positioned above this new `if (isError)` block, not below it.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/task-detail/__tests__/index.test.tsx`
Expected: PASS — 20 tests.

- [ ] **Step 5: Run the full frontend suite**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/panes/task-detail/index.tsx dashboard/src/panes/task-detail/__tests__/index.test.tsx
git commit -m "feat(dashboard): task-detail pane — not-found state keeps toolbar usable"
```

---

## Task 8: Manifest tests — completeness pass

**Files:**
- Modify: `dashboard/src/panes/task-detail/__tests__/manifest.test.ts`

**Interfaces:**
- Consumes: `manifest`, `taskDetailArgsSchema` (Task 1) — no production code changes in this task, test-only.
- Produces: nothing new for later tasks; closes the gap between task-detail spec §10 ("Manifest tests") and Task 1's initial 6 assertions with the remaining ones the brief calls out (unique id across registry, args validation edge cases, no shortcut collision — the latter two live at the registry level and are already covered by Task 5's `registry.test.ts`; this task adds the two manifest-local assertions not yet covered: description/icon presence and `route_scope` default).

- [ ] **Step 1: Write the failing test**

Append to `dashboard/src/panes/task-detail/__tests__/manifest.test.ts`:

```ts
describe("task-detail manifest — completeness", () => {
  it("has a non-empty description and an icon component", () => {
    expect(manifest.description.length).toBeGreaterThan(0);
    expect(manifest.icon).toBeDefined();
  });

  it("is cross-route scoped", () => {
    expect(manifest.route_scope).toBe("cross-route");
  });

  it("rejects a non-string taskId", () => {
    const result = taskDetailArgsSchema.safeParse({ taskId: 123 });
    expect(result.success).toBe(false);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/task-detail/__tests__/manifest.test.ts`
Expected: FAIL if `manifest.route_scope` were ever omitted — but since Task 1's manifest already sets `route_scope: "cross-route"`, run this first to confirm; if it unexpectedly passes immediately, that's fine (Task 1's manifest already satisfies it) — proceed straight to Step 4's verification without a code change. Confirm by running once before assuming a code change is needed.

- [ ] **Step 3: (only if Step 2 failed) fix `dashboard/src/panes/task-detail/manifest.ts`**

No change is expected to be necessary — Task 1's manifest already sets every field this test checks. If Step 2 failed, re-read Task 1 Step 7's manifest content and reconcile any drift introduced in a later task before proceeding.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/task-detail/__tests__/manifest.test.ts`
Expected: PASS — 9 tests total in this file.

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/panes/task-detail/__tests__/manifest.test.ts
git commit -m "test(dashboard): task-detail manifest — description/icon/route_scope/type coverage"
```

---

## Task 9: Component tests — close-affordance and rendering completeness pass

**Files:**
- Modify: `dashboard/src/panes/task-detail/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `close` prop from `PaneViewProps` (Task 1) — this task adds the one required-by-brief assertion not yet covered anywhere: that this view has no close button of its own (the shell header owns `×` per interface spec §5), so `close` is never called directly by `TaskDetailPane`. This is a deliberate negative assertion, not a placeholder.
- Produces: nothing new for later tasks.

- [ ] **Step 1: Write the failing test**

Append to `dashboard/src/panes/task-detail/__tests__/index.test.tsx`:

```tsx
describe("TaskDetailPane — close affordance", () => {
  it("never calls close() itself — the shell header owns the × button", () => {
    mockUseTask.mockReturnValue({ data: fixtureTask, isLoading: false, isError: false });
    const close = vi.fn();
    renderWithRouter(<TaskDetailPane {...noopProps()} close={close} />);
    expect(screen.queryByRole("button", { name: /^close$/i })).not.toBeInTheDocument();
    expect(close).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/task-detail/__tests__/index.test.tsx`
Expected: this assertion should already pass against the current implementation (no code in this view calls `close` or renders a "Close" button) — run it to confirm the negative assertion holds rather than expecting a genuine RED step. If it fails, it means an earlier task accidentally rendered a close button or called `close()`; fix that regression before proceeding (there is no legitimate reason for this view to own a close affordance per interface spec §5).

- [ ] **Step 3: (expected no-op) confirm no production code change needed**

Re-run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/task-detail/__tests__/index.test.tsx`
Expected: PASS — 22 tests total in this file.

- [ ] **Step 4: Run the full frontend suite one more time**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run`
Expected: PASS — every suite green, no `console.error` from React (act warnings) in the output.

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/panes/task-detail/__tests__/index.test.tsx
git commit -m "test(dashboard): task-detail pane — assert no self-owned close affordance"
```

---

## Task 10: Registry parity test (frontend ↔ backend)

**Files:**
- Create: `tests/test_pane_registry_parity.py`
- Create: `dashboard/scripts/export-pane-ids.mjs`

**Interfaces:**
- Consumes: `PANE_REGISTRY` from `dashboard/src/panes/registry.ts` (Task 5); `SERVER_PANE_REGISTRY` from `src/panes/registry.py` (Task 2).
- Produces: `tests/test_pane_registry_parity.py::test_frontend_and_backend_registries_match` — a pytest test that shells out to a small Node script to read the frontend registry's ids and `agent_pushable` flags, then diffs them against the Python-side dict (interface spec §7's parity test).

- [ ] **Step 1: Write the failing test**

`tests/test_pane_registry_parity.py`:

```python
"""Parity check between the frontend pane registry and its Python mirror.

Interface spec (pane-plugin-interface-design.md) §7: the daemon validates
`--pane-open` frames against a hand-maintained static mirror of the
frontend's `dashboard/src/panes/registry.ts`. This test is the "sync check"
that spec calls for — it fails loudly if the two registries drift.
"""

import json
import subprocess
from pathlib import Path

from src.panes.registry import SERVER_PANE_REGISTRY

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPORT_SCRIPT = REPO_ROOT / "dashboard" / "scripts" / "export-pane-ids.mjs"


def _read_frontend_registry() -> dict[str, dict[str, object]]:
    result = subprocess.run(
        ["node", str(EXPORT_SCRIPT)],
        cwd=REPO_ROOT / "dashboard",
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_frontend_and_backend_registries_match():
    frontend = _read_frontend_registry()
    assert set(frontend.keys()) == set(SERVER_PANE_REGISTRY.keys())
    for view_id, backend_entry in SERVER_PANE_REGISTRY.items():
        assert frontend[view_id]["agent_pushable"] == backend_entry["agent_pushable"], (
            f"agent_pushable mismatch for '{view_id}': "
            f"frontend={frontend[view_id]['agent_pushable']!r} "
            f"backend={backend_entry['agent_pushable']!r}"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2 && pytest tests/test_pane_registry_parity.py -v`
Expected: FAIL — `dashboard/scripts/export-pane-ids.mjs` does not exist, `node` exits non-zero, `subprocess.run(..., check=True)` raises `CalledProcessError`.

- [ ] **Step 3: Write the export script**

`dashboard/scripts/export-pane-ids.mjs`:

```js
// Reads dashboard/src/panes/registry.ts via a transpile-on-the-fly Vite
// build in library mode, then prints {viewId: {agent_pushable}} as JSON on
// stdout. Used only by tests/test_pane_registry_parity.py — not part of
// the production build.
import { build } from "vite";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const entry = path.resolve(here, "../src/panes/registry.ts");

const result = await build({
  configFile: false,
  logLevel: "silent",
  build: {
    write: false,
    lib: { entry, formats: ["es"], fileName: () => "registry.mjs" },
    rollupOptions: { external: [/^react/, /^@heroicons/, "zod"] },
  },
});

const code = result[0].output[0].code;
const tmpPath = path.join(here, "._pane-registry-export.mjs");
const fs = await import("node:fs/promises");
await fs.writeFile(tmpPath, code);

try {
  const mod = await import(`${tmpPath}?t=${Date.now()}`);
  const out = {};
  for (const [id, entryVal] of Object.entries(mod.PANE_REGISTRY)) {
    out[id] = { agent_pushable: entryVal.manifest.agent_pushable ?? true };
  }
  process.stdout.write(JSON.stringify(out));
} finally {
  await fs.unlink(tmpPath);
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2 && pytest tests/test_pane_registry_parity.py -v`
Expected: PASS — 1 test.

- [ ] **Step 5: Sanity-check the export script directly**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && node scripts/export-pane-ids.mjs`
Expected: prints `{"task-detail":{"agent_pushable":true}}`.

- [ ] **Step 6: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add tests/test_pane_registry_parity.py dashboard/scripts/export-pane-ids.mjs
git commit -m "test(panes): frontend/backend pane registry parity check"
```

---

## Task 11: Retire `TaskSidebar.tsx`, wire graph node-click to `pane.open`

**Files:**
- Modify: `dashboard/src/pages/command-center/GraphCanvas.tsx` (or wherever the graph's node-click handler and `<TaskSidebar>` mount currently live — confirm exact call site with `grep -rn "TaskSidebar" dashboard/src/pages/command-center/` before editing)
- Delete: `dashboard/src/pages/command-center/TaskSidebar.tsx`
- Modify: `dashboard/src/pages/command-center/__tests__/*.test.tsx` (whichever file(s) currently import/reference `TaskSidebar` — confirm with the same grep)

**Interfaces:**
- Consumes: `PANE_REGISTRY` is not imported here directly — the graph calls `useShellPane().open("task-detail", { taskId })` (Task 5/3), the same shell hook this view itself uses internally.
- Produces: node-click on the Command Center graph opens the shell pane instead of the retired inline sidebar; no other consumer of `TaskSidebar.tsx` remains.

Per task-detail spec §2 non-goals, "wiring the Command Center graph's node-click handler to `pane.open`" is officially Command Center consolidation (shell spec Phase D) scope, called out as a *separate PR that depends on this view*. The task list this plan was asked to cover explicitly includes it as item 11, so it is included here as this plan's final implementation task — but it is scoped narrowly to only the node-click wiring and `TaskSidebar.tsx` deletion, not any other Phase D work (tab strip, `WorkTasks`/`WorkAgents` migration, legacy redirects), which stay out of scope.

- [ ] **Step 1: Locate every reference to `TaskSidebar`**

Run: `cd /home/jkern/dev/agent-queue2 && grep -rn "TaskSidebar" dashboard/src`
Expected output (current state, to be fully removed by this task):
```
dashboard/src/pages/command-center/TaskSidebar.tsx:<multiple lines — the file itself>
dashboard/src/pages/command-center/<graph page file>.tsx:<one or two lines — import + JSX usage>
```
Read the graph page file at the reported line numbers to find the exact `onNodeClick` handler and the `<TaskSidebar ... />` JSX before editing (file name intentionally left for the executor to confirm here rather than hard-coded, since it may be `GraphCanvas.tsx`, a page-level `CommandCenterGraph.tsx`, or similar — grep it, don't guess).

- [ ] **Step 2: Write/update the failing test**

In whichever test file currently covers the graph page's node-click behavior (find via `grep -rln "TaskSidebar\|onNodeClick" dashboard/src/pages/command-center/__tests__/` — if no such test file exists yet, create `dashboard/src/pages/command-center/__tests__/graph-node-click.test.tsx`), add:

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const openMock = vi.fn();
vi.mock("../../../shell/useShellPane", () => ({
  useShellPane: () => ({ open: openMock, close: vi.fn(), state: { kind: "closed" } }),
}));

// Import path below must match the actual graph page component found in Step 1.
import GraphTab from "../GraphCanvas";

describe("Command Center graph — node click", () => {
  it("clicking a task node opens the task-detail pane instead of an inline sidebar", () => {
    render(
      <MemoryRouter>
        <GraphTab />
      </MemoryRouter>,
    );
    // Exact node-selection trigger depends on GraphCanvas's rendered DOM —
    // confirm the real test selector against the component located in Step 1
    // (e.g. a `[data-testid="task-node-<id>"]` or React Flow's node wrapper)
    // and replace the placeholder click target below before running.
    const node = screen.getByTestId(/task-node-/);
    node.click();
    expect(openMock).toHaveBeenCalledWith("task-detail", { taskId: expect.any(String) });
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/pages/command-center/__tests__/graph-node-click.test.tsx`
Expected: FAIL — `openMock` not called (current handler still opens the inline `TaskSidebar` via local component state, not the shell pane).

- [ ] **Step 4: Replace the node-click handler and remove the inline sidebar**

In the graph page file located in Step 1: remove the `import TaskSidebar from "./TaskSidebar";` line, remove the `<TaskSidebar taskId={...} gates={...} onResolveGate={...} onClose={...} />` JSX and whatever local `selectedTaskId`/`onClose` state existed solely to drive it, add `import { useShellPane } from "../../shell/useShellPane";`, and change the node-click handler body from setting local sidebar state to:

```ts
const { open } = useShellPane();
// ...inside the existing onNodeClick callback, replace the body with:
open("task-detail", { taskId: node.id });
```

Keep the graph's own node-selection visual state (if any exists for highlighting the clicked node) — only the sidebar-mounting state is removed, per task-detail spec §1's framing that this view *replaces* `TaskSidebar`, not the graph's selection highlighting.

- [ ] **Step 5: Delete `TaskSidebar.tsx`**

```bash
cd /home/jkern/dev/agent-queue2
git rm dashboard/src/pages/command-center/TaskSidebar.tsx
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/pages/command-center/__tests__/graph-node-click.test.tsx`
Expected: PASS.

- [ ] **Step 7: Run the full frontend suite and typecheck**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run && npm run typecheck`
Expected: both PASS — `typecheck` in particular confirms no dangling import of the deleted `TaskSidebar.tsx` remains anywhere (e.g. `GraphGate` type re-exported from `./types` that `TaskSidebar.tsx` used to import — confirm `dashboard/src/pages/command-center/types.ts` still exports what the graph page needs independent of the deleted file).

- [ ] **Step 8: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add -A dashboard/src/pages/command-center
git commit -m "refactor(dashboard): retire TaskSidebar — graph node-click opens task-detail pane"
```

---

## Task 12: Manual verification checklist

No automated steps — this task is a human-run confirmation pass before considering `task-detail` shippable, per shell spec §9.3's acknowledgment that this repo has no E2E infra.

- [ ] **Step 1: Start the daemon and dashboard dev server**

```bash
cd /home/jkern/dev/agent-queue2
./run.sh start
cd dashboard && npm run dev
```

- [ ] **Step 2: Manual checks**

- [ ] Open the Command Center graph, click a task node — the shell pane opens on the right showing that task's title, status badge, and metadata, and no inline sidebar renders in its place.
- [ ] With a task that has `AWAITING_APPROVAL` status, the Actions bar shows "Approve" and clicking it transitions the task (verify via the badge updating within ~60s or on manual refresh, matching `useTask`'s `refetchInterval`).
- [ ] With a task that has one or more open `human` gates referencing it, the Gates section renders exactly those gates (not gates for other tasks) with working Approve/Reject buttons.
- [ ] Click a Subtasks/Depends on/Blocks row — the pane's content swaps to the clicked task without closing/reopening (no visible flicker), and the pane header's task id updates.
- [ ] Click the toolbar's "Open full detail page" — navigates to `/tasks/:id`; the pane stays open (route change doesn't close a `cross-route` view).
- [ ] Click the toolbar's "Copy id" — paste from clipboard confirms the exact task id was copied.
- [ ] With the pane focused, press `o` — navigates to `/tasks/:id` (same as the toolbar button).
- [ ] With the pane focused, press `c` — opens the delete confirmation modal; Cancel closes it without deleting.
- [ ] With the pane focused, press `r` on a `COMPLETED` or `FAILED` task — opens the reopen-with-feedback modal; submitting with text closes the modal and the task's status changes.
- [ ] With the pane focused, press `.` — opens the more-actions menu with "Open full detail page" / "Reopen with feedback" / "Delete"; clicking outside the menu closes it.
- [ ] Navigate to a nonexistent task id (e.g. manually call `open("task-detail", { taskId: "does-not-exist" })` from the browser console via any exposed shell dev hook, or click a stale link) — "Task not found." renders, and "Open full detail page" is still present and clickable.
- [ ] Send an `aq message send --to user --to-id dashboard --thread dashboard:global --body "test" --pane-open '{"view": "task-detail", "args": {"taskId": "<a real task id>"}}'` from the CLI once the messaging/pane-open wiring from the interface spec lands (if it hasn't yet, skip this check and note it as blocked on that shell-side work) — confirms the server-side `src/panes/registry.py` entry accepts the frame.
- [ ] `pytest tests/test_pane_registry.py tests/test_pane_registry_parity.py -v` — both green in the same run as the frontend suite, confirming no drift was introduced by manual testing changes.

- [ ] **Step 3: Record results**

No commit for this task — it's a checklist, not code. If any check fails, open a follow-up task against the specific failing behavior rather than silently patching it outside this plan's task boundaries.

---

## Self-Review

**1. Spec coverage.**

- Manifest (task-detail spec §3) → Task 1.
- Args + validation (§4) → Task 1 (schema), Task 8 (edge-case tests).
- Component §5.1 data hooks → Tasks 3–6.
- Component §5.2 sections 1–7 (title block, actions bar, description, metadata grid, PR link, gates, subtasks/deps/blocks) → Tasks 3, 4, 5 respectively.
- §5.3 width behavior — no dedicated task; this is a pure CSS/Tailwind responsive rule (`sm:grid-cols-2` in the metadata grid, Task 4) rather than JS logic, so it ships inline with the grid markup and isn't separately testable under jsdom (no real layout engine) — consistent with the spec's own framing as "same breakpoint pattern `TaskSidebar` already uses," which also has no dedicated width test.
- §5.4 internal navigation → Task 4 (subtask/deps/blocks + parent-task links), Task 6 (`o` shortcut vs. pane-internal `open` distinction preserved).
- §6 toolbar + shortcuts → Task 6.
- §7 data + queries, including the `useGates` `enabled` gap and the `task_ids` typing cast → Task 5.
- §8 loading/error states → Task 3 (loading), Task 7 (not-found), Task 5 (gate-fetch-failure-equivalent: empty gates array already renders no Gates section, covered by Task 5's "omits the Gates section" test).
- §9 agent-push examples → covered structurally by Task 2/Task 10 (server accepts the frame because the registry entry exists and is validated for parity); the actual WS/chat-chip wiring is explicitly out of scope (interface spec §6.5 is shared shell work, task-detail spec §2 non-goals).
- §10 tests → Tasks 1, 3, 4, 5, 6, 7, 8, 9 collectively cover every bullet listed (manifest id/args/shortcut/agent_pushable/palette, render-without-throw, toolbar nav, clipboard copy, actions-bar-reflects-status, gate approve, subtask click, not-found, loading, shortcut keys + firing).
- §11 implementation checklist → every checkbox maps onto a task: directory (1), manifest (1), index.tsx (3–6), tests (1, 3–9), registry.py entry (2), registry.test.ts pass (5), no changes to TaskSidebar/TaskDetail from *this* task's PR-equivalent (respected — Task 11 is the explicitly-separate retirement task, matching the plan brief's inclusion of it as item 11), no InlineEventCard changes (never touched anywhere in this plan).
- §12 open questions — `c` semantics resolved pragmatically in Task 6 with the same reasoning the spec itself gives; `useGates` enabling resolved in Task 5; `TaskActions` button-list duplication accepted as-is per spec's own allowance; gate `task_ids` typing gap resolved with the same local-cast workaround the spec prescribes.
- Interface spec §4.2 registry validation (id-matches-directory, uniqueness, component-exists, no shortcut collision) → Task 5's `registry.test.ts` plus the throwing checks inside `registry.ts` itself.
- Interface spec §7 server-side mirror + parity test → Tasks 2, 10.
- Shell spec §7.4 (remove inline TaskSidebar, node click dispatches `pane.open`) → Task 11.
- Task-brief items 1–12 (the fixed list this plan was asked to produce) → each has a directly corresponding task of the same number, except items are occasionally split or reordered slightly (e.g. the brief's "toolbar registration" (6) and "pane-scoped shortcuts" (7) are combined into this plan's single Task 6, since the interface spec's `setToolbar`/`setShortcuts` calls and their local modal state are inseparable at the component level; the brief's numbering otherwise maps 1:1: brief-2→Task 2, brief-3→Task 3, brief-4→Task 4, brief-5→Task 5's gates section, brief-8→Task 8, brief-9→Task 9, brief-10→Task 10, brief-11→Task 11, brief-12→Task 12).

**2. Placeholder scan.** Searched for "TBD", "similar to", "add error handling", "handle edge cases", bare prose-only steps. None found except the deliberately-flagged, spec-inherited open question in Task 6 about `c` semantics (which is a documented design ambiguity carried from the spec itself, not an implementation placeholder — it has concrete code either way) and Task 11's Step 1/2, where the exact graph-page filename and node-click DOM selector are left for the executor to confirm via a literal `grep`/read command before editing, because that file wasn't read during plan-writing (it lives outside the three input specs and wasn't part of this plan's read set) — this is a "confirm via command" instruction, not an unresolved placeholder, and Step 1 gives the exact grep to run.

**3. Type consistency.** `PaneManifest<TArgs>`, `PaneViewProps<TArgs>`, `PaneToolbarAction`, `ShortcutBinding`, `PaneEntry` (Task 1) are used identically in Tasks 3–6, 10. `taskDetailArgsSchema` / `TaskDetailArgs` (Task 1) flow unchanged into Tasks 3–6, 8, 10. `useShellPane()` returns `{ open, close, state }` consistently from Task 3's placeholder through every later consumption in Tasks 4, 6, 11. `SERVER_PANE_REGISTRY: dict[str, dict[str, object]]` (Task 2) matches the shape `export-pane-ids.mjs` (Task 10) emits (`{view_id: {agent_pushable: bool}}`). `TaskDetailPane`'s final prop destructuring in Task 6/7 (`args, setToolbar, setShortcuts` plus later `isError`) is a strict superset of Task 3's initial `{ args }` — no renamed or dropped fields between tasks.
