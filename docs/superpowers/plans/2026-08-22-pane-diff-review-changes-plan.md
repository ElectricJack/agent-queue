# Pane View — `diff-review-changes` (v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `diff-review-changes` pane view — a file-list-left /
preview-right worktree diff surface, hosted in the shell's right-side
pane, reusing the existing `fetchTaskFiles` / `fetchTaskFileText` data
functions and `MarkdownPreview` renderer that already power the
full-page `/tasks/:id/files` route.

**Architecture:** A self-contained directory under
`dashboard/src/panes/diff-review-changes/` implementing the pane-plugin
contract (`manifest.ts` + `index.tsx` + `__tests__/`). This is the
**first** pane view landing in this repo, so this plan also creates the
two small shared files every future pane view will import
(`dashboard/src/panes/types.ts` on the frontend, `src/panes/registry.py`
+ its parity test on the backend) and stands up the dashboard's vitest +
React Testing Library test infrastructure, none of which exist yet.
Everything else (shell mounting, frontend `registry.ts`'s
`import.meta.glob` assembly, agent-push wiring, palette rendering) is
out of scope — those are Phase B shell-foundation and cross-cutting
plugin-interface concerns this view does not implement or block on.

**Tech Stack:** React 19, TypeScript (strict), Zod, TanStack Query v5,
Tailwind v4, `@heroicons/react/24/outline`, `react-router-dom` v7,
Vitest + React Testing Library (new to this repo), pytest (backend
parity test).

**Spec:**
- `docs/superpowers/specs/2026-08-22-pane-diff-review-changes-design.md`
  (this view's contract — primary source for this plan)
- `docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md`
  (manifest schema, component contract, registry mechanics)
- `docs/superpowers/specs/2026-08-22-dashboard-shell-v2-design.md`
  (shell primitives this view is hosted by, not building)

## Global Constraints

- Icons: `@heroicons/react/24/outline` (or `/solid` where the design
  calls for it). Don't introduce other icon libraries — this
  specifically means `PaneManifest.icon` and `PaneToolbarAction.icon`
  are typed as the heroicons component type, **not** `LucideIcon`
  (diff-review-changes spec §3; dashboard/CLAUDE.md).
- Never call `fetch` directly for daemon endpoints — import the SDK
  function from `../api/client`, or for routes outside the generated
  client (like `/files` and `/file`), use the existing `legacyFetch`-backed
  functions in `dashboard/src/api/taskFiles.ts` (dashboard/CLAUDE.md).
- React Query keys: `[entity, ...filters]` (dashboard/CLAUDE.md). This
  view reuses `["taskFiles", taskId]` / `["taskFile", taskId, selected]`
  verbatim — same keys as `TaskFilesPanel.tsx` — so the pane and the
  full-page route share one cache entry instead of double-fetching
  (diff-review-changes spec §7.1).
- Errors: the client interceptor throws on non-2xx; use React Query's
  `error` / `isError`, don't check `result.error` on the success branch
  (dashboard/CLAUDE.md).
- Not touching `TaskFilesPanel.tsx`'s or `TaskFiles.tsx`'s behavior —
  only promoting one existing internal function to a named export
  (diff-review-changes spec §2, §5.2).
- Not adding syntax highlighting or real unified-diff hunks — whole-file
  `<pre>` / `<MarkdownPreview>` preview only, same fidelity as
  `TaskFilesPanel.tsx` today (diff-review-changes spec §2).
- Default pane width 480px (200px min / 800px max) — this view's layout
  is tuned for that width, with a narrow-collapse breakpoint at 400px
  container width (diff-review-changes spec §5.1; shell spec §3.3).
- A pane view never touches shell code — shell mounting, routing,
  palette wiring, and agent-push handling are provided by the shell
  (plugin-interface spec §10).
- `manifest.id` must match the directory name (`diff-review-changes`).

---

## Context for the implementer

Three things this repo does **not** yet have, which this plan must
create as prerequisites before the view itself can be tested:

1. **No `zod` dependency.** `dashboard/package.json` has no `zod` entry.
   Task 1 adds it.
2. **No test runner in `dashboard/`.** There is no `vitest`, no
   `@testing-library/react`, no `*.test.tsx` file anywhere in
   `dashboard/src/`, and no `test` npm script. Task 1 stands up Vitest +
   RTL + jsdom from scratch.
3. **No `dashboard/src/panes/` directory and no `src/panes/` package.**
   This is the first pane view. Task 1 creates the shared
   `dashboard/src/panes/types.ts` (per diff-review-changes spec §3's
   note: "whichever view lands the shared `types.ts` file first"). Task
   2 creates the shared `src/panes/` Python package.

The existing full-page implementation this view reuses lives at:
- `dashboard/src/components/TaskFilesPanel.tsx` (row markup, preview
  branching, `statusColor` — currently module-private)
- `dashboard/src/pages/TaskFiles.tsx` (the `/tasks/:id/files` route,
  unmodified by this plan except as a navigation target)
- `dashboard/src/api/taskFiles.ts` (`fetchTaskFiles`, `fetchTaskFileText`,
  `TaskFileEntry`, `TaskFilesResponse` — unmodified, imported as-is)
- `dashboard/src/components/MarkdownPreview.tsx` (unmodified, imported
  as-is)

---

### Task 1: Vitest/RTL test infra + shared pane types + manifest

**Files:**
- Modify: `dashboard/package.json`
- Create: `dashboard/vitest.config.ts`
- Create: `dashboard/src/test-setup.ts`
- Create: `dashboard/src/panes/types.ts`
- Create: `dashboard/src/panes/diff-review-changes/manifest.ts`
- Test: `dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`
  (manifest block only — component block added in later tasks)

**Interfaces:**
- Produces: `HeroIcon` type, `PaneManifest<TArgs>` interface,
  `PaneViewProps<TArgs>` interface, `PaneToolbarAction` interface,
  `ShortcutBinding` interface — all exported from
  `dashboard/src/panes/types.ts`. Every later task in this plan imports
  from this file.
- Produces: `manifest` (named export, type `PaneManifest<DiffReviewChangesArgs>`),
  `diffReviewChangesArgsSchema` (Zod schema), `DiffReviewChangesArgs`
  (inferred type) — all from
  `dashboard/src/panes/diff-review-changes/manifest.ts`. Task 4 onward
  imports `DiffReviewChangesArgs`.

- [ ] **Step 1: Add `zod` and the Vitest/RTL toolchain to `dashboard/package.json`**

Edit `dashboard/package.json`: add to `"dependencies"`:

```json
    "zod": "^3.24.1"
```

Add to `"devDependencies"`:

```json
    "@testing-library/jest-dom": "^6.6.3",
    "@testing-library/react": "^16.1.0",
    "@testing-library/user-event": "^14.5.2",
    "jsdom": "^25.0.1",
    "vitest": "^2.1.8"
```

Add to `"scripts"`:

```json
    "test": "vitest run",
    "test:watch": "vitest"
```

- [ ] **Step 2: Install**

Run from the repo root (npm workspaces — `dashboard` is a workspace
member, so a root-level install updates its `node_modules` and the
shared `package-lock.json`):

```bash
npm install
```

Expected: installs without error; `dashboard/node_modules/zod` and
`dashboard/node_modules/vitest` (or their hoisted equivalents at the
workspace root `node_modules/`) exist afterward.

- [ ] **Step 3: Create the Vitest config**

Create `dashboard/vitest.config.ts`:

```ts
import { defineConfig, mergeConfig } from "vitest/config";
import viteConfig from "./vite.config";

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test-setup.ts"],
    },
  }),
);
```

- [ ] **Step 4: Create the test setup file**

Create `dashboard/src/test-setup.ts`:

```ts
import "@testing-library/jest-dom/vitest";

// jsdom doesn't implement ResizeObserver. Individual tests that need to
// drive resize callbacks (see the narrow-pane-collapse test in Task 10)
// install their own mock via `vi.stubGlobal`; this default no-op keeps
// every other test from crashing on mount.
class NoopResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

if (!("ResizeObserver" in globalThis)) {
  (globalThis as unknown as { ResizeObserver: typeof NoopResizeObserver }).ResizeObserver =
    NoopResizeObserver;
}
```

- [ ] **Step 5: Sanity-check the harness with a throwaway test**

Create a scratch file `dashboard/src/test-setup.sanity.test.ts`:

```ts
import { describe, expect, it } from "vitest";

describe("vitest harness", () => {
  it("runs", () => {
    expect(1 + 1).toBe(2);
  });
});
```

Run: `npm run test --workspace=dashboard`
Expected: 1 test file, 1 test, PASS.

Delete `dashboard/src/test-setup.sanity.test.ts` — it was only to prove
the harness works.

- [ ] **Step 6: Create the shared pane-view types**

Create `dashboard/src/panes/types.ts`:

```ts
/**
 * Shared types every pane view under `dashboard/src/panes/<view-id>/`
 * implements. Source of truth: docs/superpowers/specs/
 * 2026-08-22-pane-plugin-interface-design.md §4-§5.
 *
 * `icon` fields are typed against this dashboard's heroicons
 * convention, not `LucideIcon` — see dashboard/CLAUDE.md ("Icons:
 * @heroicons/react/24/outline ... Don't introduce other icon
 * libraries") and the diff-review-changes pane spec §3's note.
 */
import type { ComponentType, SVGProps } from "react";
import type { z } from "zod";

export type HeroIcon = ComponentType<SVGProps<SVGSVGElement>>;

export interface PaneManifest<TArgs = unknown> {
  /** Stable id — matches the directory name; used everywhere. */
  id: string;

  /** Human name shown in the pane header + palette. */
  name: string;

  /** Short description used in palette + cheat sheet. */
  description: string;

  /** Icon shown in header + palette. */
  icon: HeroIcon;

  /**
   * zod schema for the args object. Runtime-validated on every open
   * call. `undefined` schema means "no args required".
   */
  args_schema?: z.ZodType<TArgs>;

  /**
   * Optional keyboard shortcut that OPENS this view, normalized form
   * (e.g. "$mod-shift-d"). Omit the field entirely when a view has no
   * open shortcut — do not use literal `null`.
   */
  open_shortcut?: string;

  /**
   * How the view relates to routes. "cross-route" (default): pane
   * content persists across route navigation. "route-scoped": pane
   * closes automatically on route change.
   */
  route_scope?: "cross-route" | "route-scoped";

  /** Whether the agent may push this view via the pane_open message frame. Default true. */
  agent_pushable?: boolean;

  /** Palette action label. `null` means: not registered as a palette action. */
  palette_label?: string | null;

  /** Palette section this view's action belongs to. Ignored when palette_label is null. */
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
  /** Normalized key form, e.g. "$mod-r", or a bare key like "/" for pane-local bindings. */
  key: string;
  /** Shown in the cheat sheet. */
  label: string;
  onFire: () => void;
}

export interface PaneViewProps<TArgs = unknown> {
  /** The args object passed at open time, already zod-validated. */
  args: TArgs;

  /** Close the pane. */
  close: () => void;

  /** Update the args for THIS OPEN pane without closing + re-opening. Zod-validated. */
  setArgs: (next: TArgs) => void;

  /** Register toolbar action buttons in the pane header. Idempotent — call during render. */
  setToolbar: (actions: PaneToolbarAction[]) => void;

  /** Register per-entity shortcuts scoped to this pane (fire only while the pane holds focus). */
  setShortcuts: (bindings: ShortcutBinding[]) => void;
}
```

- [ ] **Step 7: Create the pane directory + manifest**

Create `dashboard/src/panes/diff-review-changes/manifest.ts`:

```ts
import { z } from "zod";
import { DocumentTextIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";

export const diffReviewChangesArgsSchema = z.object({
  taskId: z.string().min(1),
  base: z.string().min(1).optional(),
  filePath: z.string().min(1).optional(),
});

export type DiffReviewChangesArgs = z.infer<typeof diffReviewChangesArgsSchema>;

export const manifest: PaneManifest<DiffReviewChangesArgs> = {
  id: "diff-review-changes",
  name: "Review changes",
  description: "Task worktree diff — changed files vs base, with preview.",
  icon: DocumentTextIcon,
  args_schema: diffReviewChangesArgsSchema,
  open_shortcut: "$mod-shift-d",
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Review changes",
  palette_section: "Task",
};
```

- [ ] **Step 8: Write the manifest tests (failing first)**

Create `dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { basename, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { diffReviewChangesArgsSchema, manifest } from "../manifest";

describe("diff-review-changes manifest", () => {
  it("id matches the directory name", () => {
    const dir = basename(dirname(dirname(fileURLToPath(import.meta.url))));
    expect(manifest.id).toBe(dir);
    expect(manifest.id).toBe("diff-review-changes");
  });

  it("accepts the minimal valid args", () => {
    const result = diffReviewChangesArgsSchema.safeParse({ taskId: "t1" });
    expect(result.success).toBe(true);
  });

  it("accepts full valid args", () => {
    const result = diffReviewChangesArgsSchema.safeParse({
      taskId: "t1",
      base: "main",
      filePath: "a.ts",
    });
    expect(result.success).toBe(true);
  });

  it("rejects missing taskId", () => {
    const result = diffReviewChangesArgsSchema.safeParse({});
    expect(result.success).toBe(false);
  });

  it("rejects empty-string taskId", () => {
    const result = diffReviewChangesArgsSchema.safeParse({ taskId: "" });
    expect(result.success).toBe(false);
  });

  it("open_shortcut is a valid normalized $mod form", () => {
    expect(manifest.open_shortcut).toMatch(/^\$mod-(shift-)?[a-z0-9]$/i);
  });
});
```

This file will grow a `describe("DiffReviewChangesPane component", ...)`
block starting in Task 4; for now only the manifest block exists.

- [ ] **Step 9: Run the tests — expect PASS (manifest is real code, not a stub)**

Run: `npm run test --workspace=dashboard`
Expected: 6 tests in the manifest `describe` block, all PASS. (There is
no "write failing test first" step here because `manifest.ts` is
declarative data with no separate implementation phase — the test and
the implementation are written together, matching how the spec presents
the manifest as a fixed artifact, not incrementally-built logic.)

- [ ] **Step 10: Typecheck**

Run: `npm run typecheck --workspace=dashboard`
Expected: no errors.

- [ ] **Step 11: Commit**

```bash
git add dashboard/package.json dashboard/package-lock.json dashboard/vitest.config.ts \
  dashboard/src/test-setup.ts dashboard/src/panes/types.ts \
  dashboard/src/panes/diff-review-changes/manifest.ts \
  dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx
git commit -m "feat(dashboard): pane view test infra + diff-review-changes manifest"
```

(If the root `package-lock.json` changed instead of a per-workspace
one, `git add package-lock.json` instead — check `git status` and add
whichever lockfile `npm install` actually touched.)

---

### Task 2: Server-side pane registry mirror

**Files:**
- Create: `src/panes/__init__.py`
- Create: `src/panes/registry.py`
- Test: `tests/test_panes_registry.py`

**Interfaces:**
- Produces: `SERVER_PANE_REGISTRY: dict[str, dict[str, bool]]` from
  `src/panes/registry.py`. Task 11's parity test imports this.

- [ ] **Step 1: Write the failing test**

Create `tests/test_panes_registry.py`:

```python
from src.panes.registry import SERVER_PANE_REGISTRY


def test_diff_review_changes_is_agent_pushable():
    assert SERVER_PANE_REGISTRY["diff-review-changes"] == {"agent_pushable": True}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_panes_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.panes'`.

- [ ] **Step 3: Create the package**

Create `src/panes/__init__.py` (empty file).

Create `src/panes/registry.py`:

```python
"""Static mirror of the frontend pane-view registry.

Source of truth for the frontend side is
``dashboard/src/panes/*/manifest.ts`` (see
docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md §7).
This module is a hand-maintained companion so the daemon can validate
``aq message send --pane-open`` frames without needing to run the
frontend build. ``tests/test_pane_registry_parity.py`` (Task 11 of the
diff-review-changes pane plan) asserts the two registries stay in sync.

Adding a new pane view: add its manifest under
``dashboard/src/panes/<view-id>/manifest.ts`` AND a matching entry here
in the same PR.
"""

from __future__ import annotations

SERVER_PANE_REGISTRY: dict[str, dict[str, bool]] = {
    "diff-review-changes": {"agent_pushable": True},
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_panes_registry.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/panes/__init__.py src/panes/registry.py tests/test_panes_registry.py
git commit -m "feat: server-side pane registry mirror with diff-review-changes entry"
```

---

### Task 3: Promote `statusColor` to a named export

**Files:**
- Modify: `dashboard/src/components/TaskFilesPanel.tsx`
- Test: `dashboard/src/components/__tests__/TaskFilesPanel.test.tsx`

**Interfaces:**
- Consumes: nothing new.
- Produces: `export function statusColor(status: string): string` from
  `dashboard/src/components/TaskFilesPanel.tsx`. Task 4's `index.tsx`
  imports this.

- [ ] **Step 1: Write the failing test**

Create `dashboard/src/components/__tests__/TaskFilesPanel.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { statusColor } from "../TaskFilesPanel";

describe("statusColor", () => {
  it("colors Added green", () => {
    expect(statusColor("A")).toBe("text-green-400");
  });

  it("colors Deleted red", () => {
    expect(statusColor("D")).toBe("text-red-400");
  });

  it("colors Renamed and Copied blue", () => {
    expect(statusColor("R")).toBe("text-blue-400");
    expect(statusColor("C")).toBe("text-blue-400");
  });

  it("colors Modified and unknown statuses amber", () => {
    expect(statusColor("M")).toBe("text-amber-300");
    expect(statusColor("?")).toBe("text-amber-300");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test --workspace=dashboard -- TaskFilesPanel`
Expected: FAIL — `statusColor` is not exported from `TaskFilesPanel.tsx`
(TypeScript/import error, since the function is currently unexported
module-local).

- [ ] **Step 3: Promote the function to a named export**

In `dashboard/src/components/TaskFilesPanel.tsx`, change:

```ts
function statusColor(status: string): string {
```

to:

```ts
export function statusColor(status: string): string {
```

No other change to this file — `TaskFilesPanel`'s own usage of
`statusColor` still resolves the same way (a named export is still
callable unqualified within the same module).

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test --workspace=dashboard -- TaskFilesPanel`
Expected: PASS, 4 tests.

- [ ] **Step 5: Typecheck**

Run: `npm run typecheck --workspace=dashboard`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/components/TaskFilesPanel.tsx \
  dashboard/src/components/__tests__/TaskFilesPanel.test.tsx
git commit -m "refactor(dashboard): promote TaskFilesPanel's statusColor to a named export"
```

---

### Task 4: Layout — two-column with narrow-pane collapse

**Files:**
- Create: `dashboard/src/panes/diff-review-changes/index.tsx`
- Modify: `dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `PaneViewProps<TArgs>`, `PaneToolbarAction`, `ShortcutBinding`
  from `dashboard/src/panes/types.ts` (Task 1); `DiffReviewChangesArgs`
  from `./manifest` (Task 1); `statusColor` from
  `../../components/TaskFilesPanel` (Task 3).
- Produces: `export default function DiffReviewChangesPane(props:
  PaneViewProps<DiffReviewChangesArgs>)` from
  `dashboard/src/panes/diff-review-changes/index.tsx`. This is the
  component every later task in this plan edits in place.

This task builds the layout skeleton against a **hardcoded** file list
(no network calls yet — Task 5 wires the real queries) so the
two-column / narrow-collapse behavior can be verified in isolation
before data-fetching is layered in.

- [ ] **Step 1: Write the failing layout tests**

Append to `dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import DiffReviewChangesPane from "../index";

function noop() {}

function renderPane(args = { taskId: "t1" }) {
  return render(
    <DiffReviewChangesPane
      args={args}
      close={noop}
      setArgs={noop}
      setToolbar={noop}
      setShortcuts={noop}
    />,
  );
}

describe("DiffReviewChangesPane layout", () => {
  it("renders a two-column layout at default width", () => {
    const { container } = renderPane();
    const root = container.firstElementChild as HTMLElement;
    expect(root.className).toContain("flex");
    expect(root.className).not.toContain("flex-col");
  });
});
```

(This first assertion will pass against a hardcoded two-column render
even before `narrow` state exists — it's here to establish the render
harness pattern (`renderPane`) that every later test in this file
reuses. The narrow-specific assertion is added in Task 10 once the
`ResizeObserver`-driven behavior is wired.)

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test --workspace=dashboard -- diff-review-changes`
Expected: FAIL — `../index` has no default export yet (module not found).

- [ ] **Step 3: Write the layout skeleton**

Create `dashboard/src/panes/diff-review-changes/index.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";
import { statusColor } from "../../components/TaskFilesPanel";
import type { PaneViewProps } from "../types";
import type { DiffReviewChangesArgs } from "./manifest";

const NARROW_BREAKPOINT = 400;

interface FileRow {
  path: string;
  additions: number;
  deletions: number;
  status: string;
}

export default function DiffReviewChangesPane({
  args,
}: PaneViewProps<DiffReviewChangesArgs>) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [narrow, setNarrow] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  // Narrow-pane collapse: this view measures its own container because
  // PaneViewProps doesn't thread the shell's current pane width down as
  // a prop (diff-review-changes spec §5.1, open question #3).
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? el.clientWidth;
      setNarrow(width < NARROW_BREAKPOINT);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Placeholder data — replaced by real queries in Task 5.
  const files: FileRow[] = [];
  void args;

  return (
    <div ref={containerRef} className={"flex h-full " + (narrow ? "flex-col" : "")}>
      <div
        className={
          "flex flex-col rounded border border-gray-800 bg-gray-950 " +
          (narrow ? "max-h-[40%] w-full" : "w-2/5 min-w-[140px]")
        }
      >
        <ul className="flex-1 overflow-y-auto text-xs">
          {files.map((f) => (
            <li key={f.path}>
              <button
                onClick={() => setSelected(f.path)}
                className={
                  "flex w-full items-center gap-2 px-3 py-1 text-left font-mono " +
                  (selected === f.path ? "bg-indigo-950/60" : "hover:bg-gray-900")
                }
              >
                <span className={"w-4 " + statusColor(f.status)}>{f.status}</span>
                <span className="flex-1 truncate text-gray-200">{f.path}</span>
                <span className="text-green-400">+{f.additions}</span>
                <span className="text-red-400">-{f.deletions}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
      <div className="flex-1 rounded border border-gray-800 bg-gray-950 p-3">
        {!selected ? (
          <p className="text-sm text-gray-500">Select a file to preview.</p>
        ) : null}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test --workspace=dashboard -- diff-review-changes`
Expected: PASS (manifest tests from Task 1 still pass; new layout test
passes).

- [ ] **Step 5: Typecheck**

Run: `npm run typecheck --workspace=dashboard`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/panes/diff-review-changes/index.tsx \
  dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx
git commit -m "feat(dashboard): diff-review-changes pane layout skeleton"
```

---

### Task 5: Data hooks — wire `useTaskFiles`/`useTaskFile`-equivalent queries

Note on naming: the spec's assignment calls these "`useTaskFiles` /
`useTaskFile` from `dashboard/src/api/taskFiles.ts`" — that module
exports plain async functions (`fetchTaskFiles`, `fetchTaskFileText`),
not pre-built hooks (matching `TaskFilesPanel.tsx`'s existing pattern of
inlining `useQuery` calls rather than wrapping them — see
diff-review-changes spec §5, "No ... `hooks.ts` ... the two queries
below are simple enough to inline in `index.tsx`"). This task inlines
`useQuery` calls against those functions, exactly as `TaskFilesPanel.tsx`
does.

**Files:**
- Modify: `dashboard/src/panes/diff-review-changes/index.tsx`
- Modify: `dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `fetchTaskFiles(taskId: string): Promise<TaskFilesResponse>`,
  `fetchTaskFileText(taskId: string, path: string): Promise<{ text:
  string; status: number }>`, `TaskFileEntry`, `TaskFilesResponse` from
  `dashboard/src/api/taskFiles.ts` (existing, unmodified).
- Produces: the component now performs real data fetching; `files` is
  sourced from `filesQ.data.files` instead of a hardcoded array. Later
  tasks build on this `filesQ` / `fileQ` pair.

- [ ] **Step 1: Write the failing test**

Append to `dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`.
First add the imports and a `QueryClientProvider` wrapper this and all
subsequent component tests reuse — replace the existing `renderPane`
helper with:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as taskFilesApi from "../../../api/taskFiles";
import { vi } from "vitest";

function renderPane(args = { taskId: "t1" }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <DiffReviewChangesPane
        args={args}
        close={noop}
        setArgs={noop}
        setToolbar={noop}
        setShortcuts={noop}
      />
    </QueryClientProvider>,
  );
}
```

Then add:

```tsx
describe("DiffReviewChangesPane data fetching", () => {
  it("fetches files for the given taskId and renders the list", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [
        { path: "src/api/scope.py", additions: 5, deletions: 2, status: "M" },
      ],
      base: "main",
      workspace_path: "/tmp/ws",
    });

    renderPane({ taskId: "task-42" });

    expect(await screen.findByText("src/api/scope.py")).toBeInTheDocument();
    expect(taskFilesApi.fetchTaskFiles).toHaveBeenCalledWith("task-42");
  });

  it("shows a loading placeholder before the file list resolves", () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockReturnValue(new Promise(() => {}));
    renderPane();
    expect(screen.getByText("Loading files…")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test --workspace=dashboard -- diff-review-changes`
Expected: FAIL — the component renders an empty `files: []` array
regardless of the mock, and never shows "Loading files…".

- [ ] **Step 3: Wire the queries**

In `dashboard/src/panes/diff-review-changes/index.tsx`, add the import
and replace the placeholder data + top of the component body:

```ts
import { useQuery } from "@tanstack/react-query";
import { fetchTaskFiles, fetchTaskFileText } from "../../api/taskFiles";
```

Replace:

```ts
  // Placeholder data — replaced by real queries in Task 5.
  const files: FileRow[] = [];
  void args;
```

with:

```ts
  const filesQ = useQuery({
    queryKey: ["taskFiles", args.taskId],
    queryFn: () => fetchTaskFiles(args.taskId),
    refetchInterval: 5000,
  });

  const fileQ = useQuery({
    queryKey: ["taskFile", args.taskId, selected],
    queryFn: () => fetchTaskFileText(args.taskId, selected!),
    enabled: !!selected,
  });

  if (filesQ.isLoading) {
    return <div className="p-4 text-sm text-gray-500">Loading files…</div>;
  }

  const files: FileRow[] = filesQ.data?.files ?? [];
  void fileQ;
```

(The `FileRow` local interface can now be removed in favor of importing
`TaskFileEntry` from `../../api/taskFiles` — do that too: replace the
`interface FileRow {...}` block with `import type { TaskFileEntry }
from "../../api/taskFiles";` and use `TaskFileEntry` in place of
`FileRow` everywhere it appears.)

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test --workspace=dashboard -- diff-review-changes`
Expected: PASS.

- [ ] **Step 5: Typecheck**

Run: `npm run typecheck --workspace=dashboard`
Expected: no errors. (`fileQ` is intentionally unused past `void fileQ;`
at this point — Task 6 consumes it. `noUnusedLocals` is satisfied by
the `void` reference.)

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/panes/diff-review-changes/index.tsx \
  dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx
git commit -m "feat(dashboard): diff-review-changes pane — wire file-list and file-content queries"
```

---

### Task 6: File selection + preview switching + `filePath` arg sync

**Files:**
- Modify: `dashboard/src/panes/diff-review-changes/index.tsx`
- Modify: `dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `MarkdownPreview` from
  `dashboard/src/components/MarkdownPreview.tsx` (existing, unmodified,
  props `{ source: string }`).
- Produces: clicking a file row updates `selected` and calls
  `setArgs({ ...args, filePath: f.path })`; `args.filePath` on mount (or
  change) seeds `selected` if it matches a fetched file. Later tasks
  (7, 8) call the same `selectFile` helper this task introduces.

- [ ] **Step 1: Write the failing tests**

Append to `dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`:

```tsx
describe("DiffReviewChangesPane file selection", () => {
  function mockFiles() {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [
        { path: "src/api/scope.py", additions: 5, deletions: 2, status: "M" },
        { path: "README.md", additions: 1, deletions: 0, status: "A" },
      ],
      base: "main",
      workspace_path: "/tmp/ws",
    });
  }

  it("clicking a file row fetches and renders its content", async () => {
    mockFiles();
    vi.spyOn(taskFilesApi, "fetchTaskFileText").mockResolvedValue({
      text: "def scope(): ...",
      status: 200,
    });

    renderPane();
    const row = await screen.findByText("src/api/scope.py");
    row.click();

    expect(await screen.findByText("def scope(): ...")).toBeInTheDocument();
    expect(taskFilesApi.fetchTaskFileText).toHaveBeenCalledWith("t1", "src/api/scope.py");
  });

  it("clicking a second row replaces the previewed content", async () => {
    mockFiles();
    const fetchText = vi.spyOn(taskFilesApi, "fetchTaskFileText");
    fetchText.mockResolvedValueOnce({ text: "first file body", status: 200 });
    fetchText.mockResolvedValueOnce({ text: "second file body", status: 200 });

    renderPane();
    (await screen.findByText("src/api/scope.py")).click();
    expect(await screen.findByText("first file body")).toBeInTheDocument();

    (await screen.findByText("README.md")).click();
    expect(await screen.findByText("second file body")).toBeInTheDocument();
    expect(screen.queryByText("first file body")).not.toBeInTheDocument();
  });

  it("renders .md files through MarkdownPreview", async () => {
    mockFiles();
    vi.spyOn(taskFilesApi, "fetchTaskFileText").mockResolvedValue({
      text: "# Heading\n\nbody",
      status: 200,
    });

    renderPane();
    (await screen.findByText("README.md")).click();

    expect(await screen.findByRole("heading", { name: "Heading" })).toBeInTheDocument();
  });

  it("renders non-.md files as plain <pre> text, not MarkdownPreview", async () => {
    mockFiles();
    vi.spyOn(taskFilesApi, "fetchTaskFileText").mockResolvedValue({
      text: "# not a heading, just text",
      status: 200,
    });

    renderPane();
    (await screen.findByText("src/api/scope.py")).click();

    expect(await screen.findByText("# not a heading, just text")).toBeInTheDocument();
    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
  });

  it("filePath arg pre-selects a matching file on mount", async () => {
    mockFiles();
    vi.spyOn(taskFilesApi, "fetchTaskFileText").mockResolvedValue({
      text: "preselected body",
      status: 200,
    });

    renderPane({ taskId: "t1", filePath: "README.md" });

    expect(await screen.findByText("preselected body")).toBeInTheDocument();
  });

  it("filePath arg with no matching file leaves nothing selected", async () => {
    mockFiles();
    const fetchText = vi.spyOn(taskFilesApi, "fetchTaskFileText");

    renderPane({ taskId: "t1", filePath: "does/not/exist.ts" });

    await screen.findByText("src/api/scope.py");
    expect(screen.getByText("Select a file to preview.")).toBeInTheDocument();
    expect(fetchText).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test --workspace=dashboard -- diff-review-changes`
Expected: FAIL — clicking a row does not fetch content (the preview
column has no branching yet beyond the "select a file" placeholder),
and there is no `filePath`-arg-sync effect.

- [ ] **Step 3: Implement selection, preview branching, and arg sync**

In `dashboard/src/panes/diff-review-changes/index.tsx`, add the import:

```ts
import MarkdownPreview from "../../components/MarkdownPreview";
```

Change the component signature to destructure `setArgs`:

```ts
export default function DiffReviewChangesPane({
  args,
  setArgs,
}: PaneViewProps<DiffReviewChangesArgs>) {
```

Add the `filePath`-arg-sync effect, right after the narrow-pane
`useEffect`:

```ts
  useEffect(() => {
    if (!args.filePath) return;
    if (!filesQ.data?.files.some((f) => f.path === args.filePath)) return;
    setSelected(args.filePath);
  }, [args.filePath, filesQ.data]);
```

Add a `selectFile` helper right after the `files` declaration:

```ts
  function selectFile(f: TaskFileEntry) {
    setSelected(f.path);
    setArgs({ ...args, filePath: f.path });
  }
```

Change the row's `onClick` from `() => setSelected(f.path)` to
`() => selectFile(f)`.

Replace the preview column's body — from:

```tsx
      <div className="flex-1 rounded border border-gray-800 bg-gray-950 p-3">
        {!selected ? (
          <p className="text-sm text-gray-500">Select a file to preview.</p>
        ) : null}
      </div>
```

to:

```tsx
      <div className="flex-1 rounded border border-gray-800 bg-gray-950 p-3">
        {!selected ? (
          <p className="text-sm text-gray-500">Select a file to preview.</p>
        ) : fileQ.isLoading ? (
          <p className="text-sm text-gray-500">Loading {selected}…</p>
        ) : fileQ.error ? (
          <p className="text-sm text-red-400">{(fileQ.error as Error).message}</p>
        ) : selected.toLowerCase().endsWith(".md") && fileQ.data?.status === 200 ? (
          <MarkdownPreview source={fileQ.data.text} />
        ) : (
          <pre className="max-h-full overflow-auto whitespace-pre-wrap font-mono text-xs text-gray-200">
            {fileQ.data?.text}
          </pre>
        )}
      </div>
```

Remove the now-stale `void fileQ;` line from Task 5 (it's actively used
now).

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test --workspace=dashboard -- diff-review-changes`
Expected: PASS.

- [ ] **Step 5: Typecheck**

Run: `npm run typecheck --workspace=dashboard`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/panes/diff-review-changes/index.tsx \
  dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx
git commit -m "feat(dashboard): diff-review-changes pane — file selection, preview switching, filePath arg sync"
```

---

### Task 7: Toolbar — `[Refresh]`, `[Copy file path]`, `[Open full-page view]`

**Files:**
- Modify: `dashboard/src/panes/diff-review-changes/index.tsx`
- Modify: `dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `setToolbar` (from `PaneViewProps`, already in the
  destructure list per Task 6 — add it now); `useNavigate` from
  `react-router-dom` (existing dependency).
- Produces: three `PaneToolbarAction` entries registered on every
  render: `id: "refresh"`, `id: "copy-path"`, `id: "open-full-page"`.
  Task 10's tests invoke these by capturing the array `setToolbar` was
  called with.

- [ ] **Step 1: Write the failing tests**

Append to `dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`.
First add a `useNavigate` mock at the top of the file (module-level,
alongside the other imports):

```tsx
const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});
```

Update the `renderPane` helper's `setToolbar` prop to capture calls —
change the `setToolbar={noop}` line to accept an injectable spy:

```tsx
function renderPane(
  args = { taskId: "t1" },
  overrides: Partial<{ setToolbar: (actions: unknown[]) => void }> = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <DiffReviewChangesPane
        args={args}
        close={noop}
        setArgs={noop}
        setToolbar={overrides.setToolbar ?? noop}
        setShortcuts={noop}
      />
    </QueryClientProvider>,
  );
}
```

Then add:

```tsx
describe("DiffReviewChangesPane toolbar", () => {
  function mockFiles() {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [{ path: "a.ts", additions: 1, deletions: 0, status: "M" }],
      base: "main",
      workspace_path: "/tmp/ws",
    });
  }

  it("registers Refresh, Copy file path, and Open full-page view", async () => {
    mockFiles();
    const setToolbar = vi.fn();
    renderPane({ taskId: "t1" }, { setToolbar });
    await screen.findByText("a.ts");

    const lastCall = setToolbar.mock.calls.at(-1)![0] as { id: string; disabled?: boolean }[];
    const ids = lastCall.map((a) => a.id);
    expect(ids).toEqual(["refresh", "copy-path", "open-full-page"]);
  });

  it("Copy file path is disabled until a file is selected", async () => {
    mockFiles();
    const setToolbar = vi.fn();
    renderPane({ taskId: "t1" }, { setToolbar });
    await screen.findByText("a.ts");

    let lastCall = setToolbar.mock.calls.at(-1)![0] as { id: string; disabled?: boolean }[];
    expect(lastCall.find((a) => a.id === "copy-path")?.disabled).toBe(true);

    screen.getByText("a.ts").click();
    await screen.findByText(/./, { selector: "pre" });

    lastCall = setToolbar.mock.calls.at(-1)![0] as { id: string; disabled?: boolean }[];
    expect(lastCall.find((a) => a.id === "copy-path")?.disabled).toBe(false);
  });

  it("Refresh re-runs the file-list fetch", async () => {
    mockFiles();
    const setToolbar = vi.fn();
    renderPane({ taskId: "t1" }, { setToolbar });
    await screen.findByText("a.ts");
    const callsBefore = (taskFilesApi.fetchTaskFiles as ReturnType<typeof vi.fn>).mock.calls.length;

    const lastCall = setToolbar.mock.calls.at(-1)![0] as { id: string; onClick: () => void }[];
    lastCall.find((a) => a.id === "refresh")!.onClick();

    await vi.waitFor(() => {
      expect((taskFilesApi.fetchTaskFiles as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(
        callsBefore,
      );
    });
  });

  it("Open full-page view navigates to /tasks/:id/files", async () => {
    mockFiles();
    const setToolbar = vi.fn();
    renderPane({ taskId: "abc-123" }, { setToolbar });
    await screen.findByText("a.ts");

    const lastCall = setToolbar.mock.calls.at(-1)![0] as { id: string; onClick: () => void }[];
    lastCall.find((a) => a.id === "open-full-page")!.onClick();

    expect(mockNavigate).toHaveBeenCalledWith("/tasks/abc-123/files");
  });
});
```

Also mock the clipboard API once, near the top of the file (module
scope, outside any `describe`):

```tsx
Object.assign(navigator, { clipboard: { writeText: vi.fn() } });
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test --workspace=dashboard -- diff-review-changes`
Expected: FAIL — `setToolbar` is never called (component doesn't
destructure or call it yet).

- [ ] **Step 3: Implement the toolbar**

In `dashboard/src/panes/diff-review-changes/index.tsx`, add imports:

```ts
import { useNavigate } from "react-router-dom";
import {
  ArrowPathIcon,
  ClipboardIcon,
  ArrowTopRightOnSquareIcon,
} from "@heroicons/react/24/outline";
```

Update the destructure to include `setToolbar`:

```ts
export default function DiffReviewChangesPane({
  args,
  setArgs,
  setToolbar,
}: PaneViewProps<DiffReviewChangesArgs>) {
  const navigate = useNavigate();
```

After the `selectFile` helper (and before the early `filesQ.isLoading`
return — toolbar registration must run on every render per the
plugin-interface contract §5.1, not conditionally past a loading guard,
so place this block **before** the `if (filesQ.isLoading)` line):

```ts
  setToolbar([
    {
      id: "refresh",
      label: "Refresh",
      icon: ArrowPathIcon,
      onClick: () => {
        filesQ.refetch();
        if (selected) fileQ.refetch();
      },
    },
    {
      id: "copy-path",
      label: "Copy file path",
      icon: ClipboardIcon,
      onClick: () => navigator.clipboard.writeText(selected ?? ""),
      disabled: !selected,
    },
    {
      id: "open-full-page",
      label: "Open full-page view",
      icon: ArrowTopRightOnSquareIcon,
      onClick: () => navigate(`/tasks/${encodeURIComponent(args.taskId)}/files`),
    },
  ]);
```

Note: this must be placed after `filesQ` and `fileQ` are declared (they
already are, from Task 5) but before the `if (filesQ.isLoading) return
...` guard, so the toolbar registers even while loading (matching
`TaskFilesPanel.tsx`'s and the shell spec's expectation that toolbar
actions like Refresh remain available during a load).

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test --workspace=dashboard -- diff-review-changes`
Expected: PASS.

- [ ] **Step 5: Typecheck**

Run: `npm run typecheck --workspace=dashboard`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/panes/diff-review-changes/index.tsx \
  dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx
git commit -m "feat(dashboard): diff-review-changes pane toolbar (refresh, copy path, open full page)"
```

---

### Task 8: Filter input + keyboard shortcuts (`↑↓`, `Enter`, `/`, `r`)

**Files:**
- Modify: `dashboard/src/panes/diff-review-changes/index.tsx`
- Modify: `dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `setShortcuts` (from `PaneViewProps`, add to destructure now).
- Produces: a filter `<input>` in the file-list column; `focusedIndex`
  cursor state independent of `selected`; five `ShortcutBinding` entries
  registered via `setShortcuts` (`ArrowUp`, `ArrowDown`, `Enter`, `/`,
  `r`). Task 10's tests exercise these bindings by invoking their
  `onFire` callbacks directly (matching how Task 7's toolbar tests
  invoke `onClick` directly, since `setShortcuts`'s actual global
  key-listener wiring is shell code this view doesn't own or test).

- [ ] **Step 1: Write the failing tests**

Append to `dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`.
Update the `renderPane` helper once more to also capture `setShortcuts`:

```tsx
function renderPane(
  args = { taskId: "t1" },
  overrides: Partial<{
    setToolbar: (actions: unknown[]) => void;
    setShortcuts: (bindings: { key: string; label: string; onFire: () => void }[]) => void;
  }> = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <DiffReviewChangesPane
        args={args}
        close={noop}
        setArgs={noop}
        setToolbar={overrides.setToolbar ?? noop}
        setShortcuts={overrides.setShortcuts ?? noop}
      />
    </QueryClientProvider>,
  );
}
```

Then add:

```tsx
describe("DiffReviewChangesPane shortcuts and filtering", () => {
  function mockFiles() {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [
        { path: "src/a.ts", additions: 1, deletions: 0, status: "M" },
        { path: "src/b.ts", additions: 2, deletions: 1, status: "M" },
        { path: "README.md", additions: 3, deletions: 0, status: "A" },
      ],
      base: "main",
      workspace_path: "/tmp/ws",
    });
  }

  it("registers ArrowUp/ArrowDown/Enter//r bindings", async () => {
    mockFiles();
    const setShortcuts = vi.fn();
    renderPane({ taskId: "t1" }, { setShortcuts });
    await screen.findByText("src/a.ts");

    const lastCall = setShortcuts.mock.calls.at(-1)![0];
    expect(lastCall.map((b) => b.key)).toEqual(["ArrowUp", "ArrowDown", "Enter", "/", "r"]);
  });

  it("ArrowDown then Enter opens the next file in the list", async () => {
    mockFiles();
    vi.spyOn(taskFilesApi, "fetchTaskFileText").mockResolvedValue({
      text: "b body",
      status: 200,
    });
    const setShortcuts = vi.fn();
    renderPane({ taskId: "t1" }, { setShortcuts });
    await screen.findByText("src/a.ts");

    const bindings = setShortcuts.mock.calls.at(-1)![0] as { key: string; onFire: () => void }[];
    bindings.find((b) => b.key === "ArrowDown")!.onFire();
    bindings.find((b) => b.key === "Enter")!.onFire();

    expect(await screen.findByText("b body")).toBeInTheDocument();
  });

  it("/ shortcut focuses the filter input", async () => {
    mockFiles();
    const setShortcuts = vi.fn();
    renderPane({ taskId: "t1" }, { setShortcuts });
    await screen.findByText("src/a.ts");

    const bindings = setShortcuts.mock.calls.at(-1)![0] as { key: string; onFire: () => void }[];
    bindings.find((b) => b.key === "/")!.onFire();

    expect(document.activeElement).toBe(screen.getByPlaceholderText("Filter files…"));
  });

  it("r shortcut refetches the file list", async () => {
    mockFiles();
    const setShortcuts = vi.fn();
    renderPane({ taskId: "t1" }, { setShortcuts });
    await screen.findByText("src/a.ts");
    const callsBefore = (taskFilesApi.fetchTaskFiles as ReturnType<typeof vi.fn>).mock.calls.length;

    const bindings = setShortcuts.mock.calls.at(-1)![0] as { key: string; onFire: () => void }[];
    bindings.find((b) => b.key === "r")!.onFire();

    await vi.waitFor(() => {
      expect((taskFilesApi.fetchTaskFiles as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(
        callsBefore,
      );
    });
  });

  it("typing in the filter input narrows the list by case-insensitive substring", async () => {
    mockFiles();
    renderPane();
    await screen.findByText("src/a.ts");

    const input = screen.getByPlaceholderText("Filter files…");
    await import("@testing-library/user-event").then(({ default: userEvent }) =>
      userEvent.type(input, "README"),
    );

    expect(screen.getByText("README.md")).toBeInTheDocument();
    expect(screen.queryByText("src/a.ts")).not.toBeInTheDocument();
    expect(screen.queryByText("src/b.ts")).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test --workspace=dashboard -- diff-review-changes`
Expected: FAIL — no filter input exists, `setShortcuts` is never
called.

- [ ] **Step 3: Implement filtering and shortcuts**

In `dashboard/src/panes/diff-review-changes/index.tsx`, update the
destructure:

```ts
export default function DiffReviewChangesPane({
  args,
  setArgs,
  setToolbar,
  setShortcuts,
}: PaneViewProps<DiffReviewChangesArgs>) {
```

Add new refs/state right after the existing `useState` declarations:

```ts
  const filterInputRef = useRef<HTMLInputElement>(null);
  const [filter, setFilter] = useState("");
  const [focusedIndex, setFocusedIndex] = useState(0);
```

Right after the `files` declaration, add the filtered view and
navigation helpers:

```ts
  const filteredFiles = filter
    ? files.filter((f) => f.path.toLowerCase().includes(filter.toLowerCase()))
    : files;

  function moveSelection(delta: number) {
    setFocusedIndex((idx) => {
      const next = idx + delta;
      if (next < 0) return 0;
      if (next >= filteredFiles.length) return Math.max(filteredFiles.length - 1, 0);
      return next;
    });
  }

  function openFocusedFile() {
    const f = filteredFiles[focusedIndex];
    if (f) selectFile(f);
  }
```

Add `setShortcuts` registration right after the `setToolbar([...])` call
from Task 7:

```ts
  setShortcuts([
    { key: "ArrowUp", label: "Previous file", onFire: () => moveSelection(-1) },
    { key: "ArrowDown", label: "Next file", onFire: () => moveSelection(1) },
    { key: "Enter", label: "Open file", onFire: openFocusedFile },
    { key: "/", label: "Filter files", onFire: () => filterInputRef.current?.focus() },
    { key: "r", label: "Refresh", onFire: () => filesQ.refetch() },
  ]);
```

Add the filter input above the file `<ul>`, and switch the list to map
over `filteredFiles` instead of `files`, adding a focused-row highlight:

```tsx
        <div className="border-b border-gray-800 p-2">
          <input
            ref={filterInputRef}
            value={filter}
            onChange={(e) => {
              setFilter(e.target.value);
              setFocusedIndex(0);
            }}
            placeholder="Filter files…"
            className="w-full rounded bg-gray-900 px-2 py-1 text-xs text-gray-200"
          />
        </div>
        <ul className="flex-1 overflow-y-auto text-xs">
          {filteredFiles.map((f, i) => (
            <li key={f.path}>
              <button
                onClick={() => selectFile(f)}
                className={
                  "flex w-full items-center gap-2 px-3 py-1 text-left font-mono " +
                  (selected === f.path
                    ? "bg-indigo-950/60"
                    : i === focusedIndex
                      ? "bg-gray-900"
                      : "hover:bg-gray-900")
                }
              >
                <span className={"w-4 " + statusColor(f.status)}>{f.status}</span>
                <span className="flex-1 truncate text-gray-200">{f.path}</span>
                <span className="text-green-400">+{f.additions}</span>
                <span className="text-red-400">-{f.deletions}</span>
              </button>
            </li>
          ))}
        </ul>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test --workspace=dashboard -- diff-review-changes`
Expected: PASS. (The "typing in the filter input" test dynamically
imports `@testing-library/user-event`, added as a dependency in Task 1
Step 1 — if that import pattern feels awkward, a static top-of-file
`import userEvent from "@testing-library/user-event";` works equally
well; either is fine, prefer the static import for consistency with the
rest of the file.)

- [ ] **Step 5: Typecheck**

Run: `npm run typecheck --workspace=dashboard`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/panes/diff-review-changes/index.tsx \
  dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx
git commit -m "feat(dashboard): diff-review-changes pane — filter input and keyboard shortcuts"
```

---

### Task 9: Loading + error states (no-workspace, not-a-git-checkout, empty, file-level errors)

**Files:**
- Modify: `dashboard/src/panes/diff-review-changes/index.tsx`
- Modify: `dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `TaskFilesResponse.reason` (`"no_workspace" |
  "not_a_git_checkout" | "diff_failed"`) from
  `dashboard/src/api/taskFiles.ts` (existing type, unmodified).
- Produces: the component's early-return branch order now fully matches
  `TaskFilesPanel.tsx`'s (diff-review-changes spec §8): loading → error
  → `no_workspace` → `not_a_git_checkout` → empty-diff → the two-column
  body. No behavior for 403/binary/404/413 needs new code — those are
  already pre-normalized strings from `fetchTaskFileText` rendered
  through the existing `<pre>` branch (Task 6); this task only adds
  tests proving that.

- [ ] **Step 1: Write the failing tests**

Append to `dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`:

```tsx
describe("DiffReviewChangesPane loading and error states", () => {
  it("shows the top-level error branch when the file list fetch rejects", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockRejectedValue(new Error("boom"));
    renderPane();
    expect(await screen.findByText("Failed to load files: boom")).toBeInTheDocument();
  });

  it("shows the no_workspace message and no file list", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [],
      base: null,
      workspace_path: null,
      reason: "no_workspace",
    });
    renderPane();
    expect(
      await screen.findByText(
        "Task has no attached workspace. Files will appear once the task acquires a worktree.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the not_a_git_checkout message with workspace_path interpolated", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [],
      base: null,
      workspace_path: "/tmp/not-git",
      reason: "not_a_git_checkout",
    });
    renderPane();
    expect(
      await screen.findByText("Task workspace (/tmp/not-git) is not a git checkout."),
    ).toBeInTheDocument();
  });

  it("shows the empty-diff message with base interpolated", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [],
      base: "main",
      workspace_path: "/tmp/ws",
    });
    renderPane();
    expect(await screen.findByText("No changes vs main yet.")).toBeInTheDocument();
  });

  it("renders a binary-file placeholder through the plain <pre> branch", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [{ path: "logo.png", additions: 0, deletions: 0, status: "A" }],
      base: "main",
      workspace_path: "/tmp/ws",
    });
    vi.spyOn(taskFilesApi, "fetchTaskFileText").mockResolvedValue({
      text: "(binary file omitted (12 KB))",
      status: 200,
    });
    renderPane();
    (await screen.findByText("logo.png")).click();
    expect(await screen.findByText("(binary file omitted (12 KB))")).toBeInTheDocument();
  });

  it("renders a forbidden-path placeholder through the plain <pre> branch", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [{ path: "secret.env", additions: 0, deletions: 0, status: "A" }],
      base: "main",
      workspace_path: "/tmp/ws",
    });
    vi.spyOn(taskFilesApi, "fetchTaskFileText").mockResolvedValue({
      text: "(forbidden path)",
      status: 403,
    });
    renderPane();
    (await screen.findByText("secret.env")).click();
    expect(await screen.findByText("(forbidden path)")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test --workspace=dashboard -- diff-review-changes`
Expected: FAIL — the component has no `filesQ.error`, `no_workspace`,
`not_a_git_checkout`, or empty-diff branches yet (it currently only
guards `filesQ.isLoading` before rendering the two-column body
unconditionally); the binary/forbidden-path tests likely already pass
(they exercise the existing `<pre>` branch from Task 6) but are included
here to lock in that behavior alongside the new branches.

- [ ] **Step 3: Add the remaining early-return branches**

In `dashboard/src/panes/diff-review-changes/index.tsx`, right after the
existing:

```ts
  if (filesQ.isLoading) {
    return <div className="p-4 text-sm text-gray-500">Loading files…</div>;
  }
```

add:

```ts
  if (filesQ.error) {
    return (
      <div className="p-4 text-sm text-red-400">
        Failed to load files: {(filesQ.error as Error).message}
      </div>
    );
  }
```

Then, right after the `const files: TaskFileEntry[] = filesQ.data?.files
?? [];` line, insert the reason-code and empty-diff branches (these need
`filesQ.data` directly, not the defaulted `files` array, to read
`.reason`, `.workspace_path`, and `.base`):

```ts
  const data = filesQ.data;
  if (data?.reason === "no_workspace") {
    return (
      <div className="p-4 text-sm text-gray-500">
        Task has no attached workspace. Files will appear once the task
        acquires a worktree.
      </div>
    );
  }
  if (data?.reason === "not_a_git_checkout") {
    return (
      <div className="p-4 text-sm text-gray-500">
        Task workspace ({data.workspace_path}) is not a git checkout.
      </div>
    );
  }
  if (data && data.files.length === 0) {
    return (
      <div className="p-4 text-sm text-gray-500">
        No changes vs {data.base} yet.
      </div>
    );
  }
```

Note on ordering: this must come **after** the `setToolbar` /
`setShortcuts` registration calls added in Tasks 7-8 (toolbar/shortcuts
register unconditionally, per the plugin-interface contract §5.1/§5.2 —
"Called during render", not gated behind these branches) but **before**
the two-column JSX return at the bottom of the function.

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test --workspace=dashboard -- diff-review-changes`
Expected: PASS.

- [ ] **Step 5: Typecheck**

Run: `npm run typecheck --workspace=dashboard`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/panes/diff-review-changes/index.tsx \
  dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx
git commit -m "feat(dashboard): diff-review-changes pane — no_workspace/not_a_git_checkout/empty-diff states"
```

---

### Task 10: Narrow-pane collapse test + remaining manifest/component test coverage

By this point the component is feature-complete (Tasks 4-9 built it
incrementally, each with its own passing tests). This task closes the
two gaps §10 of the diff-review-changes spec calls for that don't fit
naturally into an earlier task: the `ResizeObserver`-driven narrow
layout, and the `close` prop pass-through check.

**Files:**
- Modify: `dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: everything already produced by Tasks 1-9. No production
  code changes in this task — it is test-only, and Step 2 is expected to
  already pass (verifying prior tasks' work), which is why this task's
  cycle is "write test → run → confirm PASS" rather than "fail then
  pass".

- [ ] **Step 1: Write the narrow-pane-collapse test**

Append to `dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`:

```tsx
describe("DiffReviewChangesPane narrow-pane collapse", () => {
  it("switches to a stacked layout when the container is narrower than 400px", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [{ path: "a.ts", additions: 1, deletions: 0, status: "M" }],
      base: "main",
      workspace_path: "/tmp/ws",
    });

    let capturedCallback: ResizeObserverCallback | null = null;
    class CapturingResizeObserver {
      constructor(cb: ResizeObserverCallback) {
        capturedCallback = cb;
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", CapturingResizeObserver);

    const { container } = renderPane();
    await screen.findByText("a.ts");

    const root = container.firstElementChild as HTMLElement;
    expect(root.className).not.toContain("flex-col");

    capturedCallback!(
      [{ contentRect: { width: 300 } } as ResizeObserverEntry],
      undefined as unknown as ResizeObserver,
    );

    await vi.waitFor(() => {
      expect(root.className).toContain("flex-col");
    });

    vi.unstubAllGlobals();
  });
});
```

- [ ] **Step 2: Run all diff-review-changes tests**

Run: `npm run test --workspace=dashboard -- diff-review-changes`
Expected: PASS — including this new test, exercising the
`ResizeObserver` wiring from Task 4's `useEffect`, which has been in
place unchanged since Task 4 and never needed touching in Tasks 5-9.

- [ ] **Step 3: Add the `close`-prop acceptance check**

Per diff-review-changes spec §10 ("this view's tests don't need a
close-affordance test beyond confirming the prop is accepted without
being called spuriously"), append:

```tsx
describe("DiffReviewChangesPane close prop", () => {
  it("accepts the close prop without invoking it", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [{ path: "a.ts", additions: 1, deletions: 0, status: "M" }],
      base: "main",
      workspace_path: "/tmp/ws",
    });
    const close = vi.fn();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <DiffReviewChangesPane
          args={{ taskId: "t1" }}
          close={close}
          setArgs={noop}
          setToolbar={noop}
          setShortcuts={noop}
        />
      </QueryClientProvider>,
    );
    await screen.findByText("a.ts");
    expect(close).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 4: Run the full test file one more time**

Run: `npm run test --workspace=dashboard -- diff-review-changes`
Expected: every test in
`dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`
PASSes — manifest tests (Task 1), layout (Task 4), data fetching (Task
5), selection/preview/arg-sync (Task 6), toolbar (Task 7),
shortcuts/filtering (Task 8), loading/error states (Task 9), narrow
collapse and close-prop (this task).

- [ ] **Step 5: Run the full dashboard suite + typecheck once, to catch cross-file regressions**

Run:
```bash
npm run test --workspace=dashboard
npm run typecheck --workspace=dashboard
npm run lint --workspace=dashboard
```
Expected: all PASS/clean (the `TaskFilesPanel.test.tsx` suite from
Task 3 should also still be green here).

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx
git commit -m "test(dashboard): diff-review-changes pane — narrow-collapse and close-prop coverage"
```

---

### Task 11: Registry parity test

**Files:**
- Create: `tests/test_pane_registry_parity.py`

**Interfaces:**
- Consumes: `SERVER_PANE_REGISTRY` from `src/panes/registry.py` (Task 2).
  Scans `dashboard/src/panes/*/manifest.ts` on disk for `id: "..."`
  literals — no frontend build step or Node process required, so this
  test runs standalone under `pytest`.

This is the parity test from plugin-interface spec §7, scoped for the
current single-view state of the registry (`diff-review-changes` is the
only view that exists in this repo yet — every future pane view's plan
adds one more `manifest.ts` file this test will pick up automatically,
no test-file change required on their part).

- [ ] **Step 1: Write the test**

Create `tests/test_pane_registry_parity.py`:

```python
"""Confirms the frontend pane-view manifests and the backend's
SERVER_PANE_REGISTRY (src/panes/registry.py) agree on the set of view
ids.

Frontend ids are extracted by scanning `dashboard/src/panes/*/manifest.ts`
for the `id: "..."` literal rather than by running the frontend build —
this keeps the test fast and dependency-free (no Node process). Every
manifest.ts in this repo declares `id` as a plain string literal (see
docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md §4);
if a future manifest ever computes `id` dynamically, this regex-based
reader will need to grow alongside it.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.panes.registry import SERVER_PANE_REGISTRY

DASHBOARD_PANES_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "src" / "panes"
_ID_RE = re.compile(r'id:\s*"([^"]+)"')


def _read_frontend_manifest_ids() -> set[str]:
    ids: set[str] = set()
    for manifest_path in sorted(DASHBOARD_PANES_DIR.glob("*/manifest.ts")):
        text = manifest_path.read_text()
        match = _ID_RE.search(text)
        assert match, f"{manifest_path} has no `id: \"...\"` literal"
        ids.add(match.group(1))
    return ids


def test_frontend_and_backend_registries_match():
    frontend_ids = _read_frontend_manifest_ids()
    backend_ids = set(SERVER_PANE_REGISTRY.keys())
    assert frontend_ids == backend_ids


def test_diff_review_changes_is_in_both_registries():
    assert "diff-review-changes" in _read_frontend_manifest_ids()
    assert "diff-review-changes" in SERVER_PANE_REGISTRY
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/test_pane_registry_parity.py -v`
Expected: PASS, 2 tests (frontend ids = `{"diff-review-changes"}`,
backend ids = `{"diff-review-changes"}` — they match because Task 2
added the one entry that mirrors Task 1's one manifest).

- [ ] **Step 3: Commit**

```bash
git add tests/test_pane_registry_parity.py
git commit -m "test: pane registry parity — frontend manifests vs SERVER_PANE_REGISTRY"
```

---

### Task 12: Manual verification checklist

No code changes. Confirms the view behaves correctly when actually run,
since the dashboard has no E2E infra (per shell spec §9.2, "manual
verification for cross-page flows") and this pane isn't mounted inside a
real `<ShellPane>` shell yet (Phase B, not built by this plan) — so
manual verification here means exercising the component directly via a
standalone harness route, not the full shell experience.

- [ ] **Step 1: Add a temporary dev-only mount point**

This pane has no shell to mount it in yet, so temporarily wire it into
`dashboard/src/App.tsx` behind a scratch route to eyeball it. In
`dashboard/src/App.tsx`, add (near the existing `TaskFiles` lazy import):

```ts
const DiffReviewChangesPaneDevHarness = lazy(() =>
  import("./panes/diff-review-changes/index").then((m) => ({
    default: () => (
      <div style={{ height: "600px", width: "480px", border: "1px solid #333" }}>
        <m.default
          args={{ taskId: new URLSearchParams(window.location.search).get("taskId") ?? "" }}
          close={() => {}}
          setArgs={() => {}}
          setToolbar={() => {}}
          setShortcuts={() => {}}
        />
      </div>
    ),
  })),
);
```

Add a matching route: `<Route path="_dev/diff-review-changes"
element={<DiffReviewChangesPaneDevHarness />} />`.

- [ ] **Step 2: Start the daemon and the dashboard dev server**

```bash
./run.sh start
npm run dev --workspace=dashboard
```

- [ ] **Step 3: Find a real task id with a worktree diff**

```bash
aq task list --status active --json | head -20
```

Pick a `task_id` that has an attached workspace with uncommitted or
committed-since-base changes (an in-progress task worked by an agent is
the easiest source).

- [ ] **Step 4: Verify the pane against that task**

Navigate to `http://localhost:5173/_dev/diff-review-changes?taskId=<task_id>`.

Check, using the real running daemon:
- File list renders with correct status letters/colors and +/- counts.
- Clicking a `.md` file renders through `MarkdownPreview` (headings,
  tables render as HTML, not raw markdown).
- Clicking a non-`.md` file renders as plain monospace text.
- Typing in the filter input narrows the list.
- `↑`/`↓` visually move a focus highlight through the (filtered) rows;
  `Enter` opens the focused row into the preview.
- `Refresh` toolbar button re-fetches (watch the Network tab for a new
  `/files` request).
- `Copy file path` toolbar button is disabled with nothing selected,
  enabled after a selection, and puts the selected path on the system
  clipboard (paste somewhere to confirm).
- `Open full-page view` navigates to `/tasks/<task_id>/files` and that
  page still renders correctly (confirms `TaskFilesPanel.tsx` /
  `TaskFiles.tsx` are untouched and still work).
- Resize the browser window (or devtools-emulate a narrow viewport)
  down past ~400px of the harness container's width — actually easier:
  temporarily shrink the harness `<div>`'s inline `width` in Step 1 to
  `"350px"` and reload — confirm the layout stacks (file list on top,
  preview below) instead of staying side-by-side.

- [ ] **Step 5: Verify an edge-case task**

Find or create a task with no attached workspace (a freshly-created,
not-yet-picked-up task) and one whose workspace isn't a git checkout if
one exists in the environment; navigate the harness to each and confirm
the `no_workspace` / `not_a_git_checkout` messages render instead of a
crash or blank pane.

- [ ] **Step 6: Remove the temporary dev harness**

Revert the `App.tsx` changes from Step 1 — this was scaffolding only,
not part of the shipped view:

```bash
git checkout -- dashboard/src/App.tsx
```

Confirm `git status` shows `App.tsx` clean and only this plan's real
commits (Tasks 1-11) remain.

- [ ] **Step 7: Final full-suite run**

```bash
npm run test --workspace=dashboard
npm run typecheck --workspace=dashboard
npm run lint --workspace=dashboard
pytest tests/test_panes_registry.py tests/test_pane_registry_parity.py -v
```

Expected: everything green. No commit for this task — it's manual
verification only, and Step 6 already discarded the only file it
touched.

---

## Self-review

**Spec coverage** (against `2026-08-22-pane-diff-review-changes-design.md`):
- §3 Manifest → Task 1.
- §4 Args + validation → Task 1 (schema), Task 6 (`filePath` sync).
- §5.1 Layout / narrow collapse → Task 4, Task 10 (test).
- §5.2 File list row / filter / keyboard nav → Task 3 (`statusColor`),
  Task 8 (filter + nav).
- §5.3 Preview area branching → Task 6, Task 9.
- §5.4 Full-page escape hatch → Task 7.
- §6.1 Toolbar → Task 7.
- §6.2 Shortcuts → Task 8.
- §7 Data + queries, §7.1 query keys → Task 5; §7.2 `filePath` sync → Task 6.
- §8 Loading/error/edge cases (1-7) → Task 5 (loading), Task 9
  (error, no_workspace, not_a_git_checkout, empty), Task 6 (binary/403
  etc. via the shared `<pre>` branch, tested in Task 9).
- §9 Agent-push examples — explicitly not implemented by this view (the
  `--pane-open` CLI flag and WS dispatch are plugin-interface spec
  cross-cutting work); the server-side mirror entry this section asks
  for is Task 2.
- §10 Tests → distributed across Tasks 1, 3-10 (manifest tests in Task
  1; every component test bullet mapped to the task that introduces the
  behavior it tests).
- §11 Implementation checklist → every bullet maps to a task: directory
  (Task 1), `statusColor` promotion (Task 3), `index.tsx` (Tasks 4-9),
  server registry (Task 2), tests (Tasks 1, 3-10), frontend registry
  parity (deferred per the checklist's own "once it exists" phrasing —
  Task 11 builds the backend-testable half without waiting on the
  not-yet-built frontend `registry.ts`), `test_pane_registry_parity.py`
  (Task 11).
- §12 Open questions — all three are explicitly deferred by the spec
  itself (`base` backend wiring, full-page selection handoff,
  width-prop vs `ResizeObserver`); this plan implements the spec's
  stated v1 behavior for each (display-only `base`, plain navigation
  losing selection, `ResizeObserver`) and does not attempt to resolve
  them, matching "not blocking for v1."

**Placeholder scan:** No TBDs, no "add error handling"-style steps, no
"similar to Task N" cross-references without inline code — every step
either shows the literal diff/file content or a literal shell command
with an expected result. Fixed one instance mid-draft where an early
version of Task 9 said "add the remaining branches" without showing
them — replaced with the actual code blocks shown above.

**Type consistency:** `DiffReviewChangesArgs` (Task 1) is the type
threaded through every later task's `args` prop. `PaneViewProps`,
`PaneToolbarAction`, `ShortcutBinding` (Task 1, `types.ts`) are the
exact interfaces Tasks 4-9 destructure against — verified `setToolbar`
signature (`(actions: PaneToolbarAction[]) => void`) matches the array
literal built in Task 7, and `setShortcuts`
(`(bindings: ShortcutBinding[]) => void`) matches Task 8's array.
`statusColor(status: string): string` (Task 3) matches its call site in
Task 4's initial skeleton. `TaskFileEntry` / `TaskFilesResponse` /
`fetchTaskFiles` / `fetchTaskFileText` signatures are copied verbatim
from the existing `dashboard/src/api/taskFiles.ts` (read directly,
not guessed) into every task that imports them (Tasks 4-6), so no
drift from the real module. `SERVER_PANE_REGISTRY: dict[str, dict[str,
bool]]` (Task 2) is read by the exact same name in Task 11's parity
test.
