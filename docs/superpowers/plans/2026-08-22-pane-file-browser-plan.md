# Pane View: `file-browser` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `file-browser` pane view (workspace-scoped directory tree
+ read-only file preview) and its two backend endpoints
(`GET /api/workspaces/{id}/browse`, `GET /api/workspaces/{id}/file`), so a
user or the global supervisor can browse any workspace's files independent
of a task's diff.

**Architecture:** Backend first (per spec's own ordering: extract a shared
`serve_workspace_relative_file` helper out of the existing
`/api/tasks/{id}/file` handler into `src/api/file_serving.py`, then build a
new `src/api/workspace_files.py` router on top of it), so the frontend has
real endpoints to point React Query at. Frontend ships as a self-contained
directory under `dashboard/src/panes/file-browser/` implementing the pane
plugin contract (manifest + component + `args`/`setArgs`/`setToolbar`/
`setShortcuts` props), tested in isolation by rendering the component
directly with mock props — it does not require the shell to be mounted.

**Tech Stack:** FastAPI (backend), React 19 + TanStack Query + Zod (frontend),
pytest + httpx.ASGITransport (backend tests), Vitest + React Testing Library
(frontend tests — net-new to this repo, installed in Task 1).

**Spec:**
- `docs/superpowers/specs/2026-08-22-dashboard-shell-v2-design.md`
- `docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md`
- `docs/superpowers/specs/2026-08-22-pane-file-browser-design.md`

## Global Constraints

- **Path safety is non-negotiable and must reuse existing logic, not
  reimplement it.** `serve_workspace_relative_file` is a behavior-preserving
  extraction of `task_files.py::get_file` (spec file-browser §8.3) — every
  existing `/api/tasks/{id}/file` test in `tests/test_api_task_files.py`
  must keep passing unmodified after the extraction.
- **512 KB file size cap** (`MAX_FILE_BYTES = 512 * 1024`), same constant,
  shared between both endpoints (spec §8.3).
- **Binary heuristic:** NUL byte in the first 8 KiB of the file → JSON
  `{"success": true, "reason": "binary", "size": N, "path": "..."}` instead
  of `text/plain` (spec §8.3, mirrors existing task-file behavior).
- **Out-of-scope workspace access returns 404, not 403** — don't leak
  workspace existence (spec §8.4, file-browser spec §9's "same rendering as
  not-found" table row).
- **No dotfile filtering, no pagination** in v1 — `browse` lists everything
  in a directory unfiltered (file-browser spec §8.2 step 5, §13). Do not add
  either; they're explicitly deferred.
- **Icon component type:** panes import icons from `@heroicons/react/24/outline`
  only — `LucideIcon` / `lucide-react` must **not** be introduced, even
  though the file-browser spec's own manifest example uses
  `from "lucide-react"` (plugin-interface spec §4 is authoritative here and
  explicitly overrides that example: "This dashboard is standardized on
  heroicons... LucideIcon must NOT be introduced"). Task 6 uses
  `FolderOpenIcon` from `@heroicons/react/24/outline`.
- **No new HTTP client abstraction.** Frontend data hooks call
  `legacyFetch` from `dashboard/src/api/legacy-fetch.ts` (same pattern as
  `dashboard/src/api/taskFiles.ts`) — per `dashboard/CLAUDE.md`, these two
  endpoints aren't in the generated `@aq/ts-client` SDK (browse's `entries`
  shape isn't modeled there and `file`'s raw-text/JSON dual response isn't
  either), so `legacy-fetch.ts` is the correct, already-established escape
  hatch — not a violation of "never call fetch directly."
- **`manifest.id` matches its directory name** (`file-browser`) — enforced
  by the shared registry validation described in plugin-interface spec
  §4.2; Task 12's parity test checks this.

## Prerequisites — repo state as of this plan (read before starting)

This repo has **not yet shipped** the dashboard-shell-v2 "Phase B" shell
foundation (`ShellPane.tsx`, `ActivityDrawer.tsx`, `useShellPane` hook) or
any prior pane view. `dashboard/src/panes/`, `dashboard/src/shell/`, and
`src/panes/registry.py` do not exist. The dashboard also has **zero**
frontend test infrastructure today (no `vitest`, no
`@testing-library/react`, no `zod`, no `@`-alias in `tsconfig.app.json` /
`vite.config.ts`).

This plan does not build the shell (`ShellPane`, `ActivityDrawer`,
`useShellPane`) — that's a separate Phase B plan. What it does do, because
file-browser is one of the first three v1 pane views and nothing has
bootstrapped the shared pane-plugin scaffolding yet:

- Task 1 installs `zod`, `vitest`, `@testing-library/react`,
  `@testing-library/jest-dom`, `jsdom`, adds a `test` script, a
  `vitest.config.ts`, and the `@` → `dashboard/src` path alias both repos
  (`tsconfig.app.json` `paths`, `vite.config.ts` `resolve.alias`) need.
- Task 2 creates `dashboard/src/shell/paneTypes.ts` — the exact type
  contract from plugin-interface spec §4/§5 (`PaneManifest`,
  `PaneViewProps`, `PaneToolbarAction`, `ShortcutBinding`). This is
  type-only, has zero runtime behavior, and is what every future pane view
  and the eventual `ShellPane.tsx` import from — landing it here doesn't
  preempt or conflict with the Phase B shell plan.
- Task 3 creates `dashboard/src/panes/registry.ts` — the
  `import.meta.glob`-based registry + validation from plugin-interface spec
  §4.1/§4.2. Generic infra, not file-browser-specific; future pane-view
  plans add a directory and get picked up automatically.
- Task 13 creates `src/panes/registry.py` — the server-side static mirror
  from plugin-interface spec §7 option A, plus the parity test.

The file-browser component itself (Task 6) is tested by rendering it
directly with hand-built props (`args`, `close`, `setArgs`, `setToolbar`,
`setShortcuts`) — it does not need `ShellPane` or `useShellPane` to exist to
be fully tested and merged. When the Phase B shell lands later, it picks up
`file-browser` automatically via the registry with no further changes here.

---

## Task 1: Frontend test + dependency scaffolding

**Files:**
- Modify: `dashboard/package.json`
- Modify: `dashboard/tsconfig.app.json`
- Modify: `dashboard/vite.config.ts`
- Create: `dashboard/vitest.config.ts`
- Create: `dashboard/src/test/setup.ts`

**Interfaces:**
- Produces: `npm test` (in `dashboard/`) runs Vitest once with jsdom +
  `@testing-library/jest-dom` matchers auto-loaded. `@/` resolves to
  `dashboard/src/` in both `tsc` and Vite/Vitest.

- [ ] **Step 1: Install dependencies**

```bash
cd dashboard
npm install zod
npm install -D vitest @testing-library/react @testing-library/jest-dom \
  @testing-library/user-event jsdom
```

- [ ] **Step 2: Add the `@` path alias to `tsconfig.app.json`**

Add `"baseUrl": "."` and `"paths"` under `compilerOptions` (keep every
existing key):

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,

    "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "isolatedModules": true,
    "moduleDetection": "force",
    "noEmit": true,
    "jsx": "react-jsx",

    "baseUrl": ".",
    "paths": {
      "@/*": ["src/*"]
    },

    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "noUncheckedIndexedAccess": true
  },
  "include": ["src"]
}
```

- [ ] **Step 3: Add the matching alias to `vite.config.ts`**

```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";
import { fileURLToPath } from "node:url";

const dirname = path.dirname(fileURLToPath(import.meta.url));

// Read via globalThis so this config typechecks without @types/node.
const target =
  (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env
    .AQ_API_TARGET ?? "http://127.0.0.1:8081";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(dirname, "src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": target,
      "/health": target,
      "/ready": target,
      "/ws": { target, ws: true },
    },
  },
});
```

- [ ] **Step 4: Create `dashboard/vitest.config.ts`**

```ts
import { defineConfig, mergeConfig } from "vitest/config";
import viteConfig from "./vite.config";

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      globals: true,
    },
  }),
);
```

- [ ] **Step 5: Create `dashboard/src/test/setup.ts`**

```ts
import "@testing-library/jest-dom/vitest";
```

- [ ] **Step 6: Add the `test` script to `package.json`**

Add `"test": "vitest run"` to the `scripts` block (alongside `dev`,
`build`, `preview`, `lint`, `typecheck`).

- [ ] **Step 7: Verify the toolchain boots**

Run: `cd dashboard && npm run test`
Expected: Vitest starts and reports "No test files found" (exit code 1 is
acceptable here — there are no test files yet; the goal is confirming the
config loads without error, not a passing suite). Also run
`npm run typecheck` and `npm run build` to confirm the path alias didn't
break the existing build.

- [ ] **Step 8: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/package.json dashboard/package-lock.json \
  dashboard/tsconfig.app.json dashboard/vite.config.ts \
  dashboard/vitest.config.ts dashboard/src/test/setup.ts
git commit -m "chore(dashboard): add Vitest + RTL + zod + @ path alias"
```

---

## Task 2: Shared pane-plugin type contract (`paneTypes.ts`)

**Files:**
- Create: `dashboard/src/shell/paneTypes.ts`

**Interfaces:**
- Produces: `PaneManifest<TArgs>`, `PaneViewProps<TArgs>`,
  `PaneToolbarAction`, `ShortcutBinding` — every pane view (this one and
  future ones) imports these from `@/shell/paneTypes`.

- [ ] **Step 1: Write `dashboard/src/shell/paneTypes.ts`**

Transcribed verbatim from plugin-interface spec §4 and §5, minus the
`z.ZodType` import cycle concern (zod is a real dependency as of Task 1):

```ts
/**
 * Pane plugin contract — the type surface every pane view under
 * dashboard/src/panes/<view-id>/ implements.
 *
 * Source of truth: docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md
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

  /** Icon shown in header + palette. Must come from @heroicons/react/24/outline. */
  icon: HeroIcon;

  /**
   * zod schema for the args object. Runtime-validated on every open call.
   * `undefined` schema means "no args required".
   */
  args_schema?: z.ZodType<TArgs>;

  /**
   * Optional keyboard shortcut that OPENS this view, e.g. "$mod-shift-D".
   * Omit the field entirely for "no open shortcut" — never a literal
   * `null`.
   */
  open_shortcut?: string;

  /**
   * How the view relates to routes.
   * - "cross-route" (default): pane content persists across route nav.
   * - "route-scoped": pane closes automatically on route change.
   */
  route_scope?: "cross-route" | "route-scoped";

  /** Whether the agent may push this view via pane_open. Default true. */
  agent_pushable?: boolean;

  /**
   * Palette action label. `null` (or omitted) means the view is not
   * registered as a palette action.
   */
  palette_label?: string | null;

  /** Palette section this view's action belongs to. Ignored if palette_label is null. */
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
  key: string; // normalized form, e.g. "$mod-r"
  label: string; // shown in cheat sheet
  onFire: () => void;
}

export interface PaneViewProps<TArgs = unknown> {
  /** The args object passed at open time, already zod-validated. */
  args: TArgs;

  /** Close the pane. */
  close: () => void;

  /** Update the args for THIS OPEN pane without closing + re-opening. */
  setArgs: (next: TArgs) => void;

  /** Register toolbar action buttons in the pane header. Passing [] clears it. */
  setToolbar: (actions: PaneToolbarAction[]) => void;

  /** Register per-entity shortcuts scoped to this pane. */
  setShortcuts: (bindings: ShortcutBinding[]) => void;
}

export interface PaneEntry {
  manifest: PaneManifest;
  Component: ComponentType<PaneViewProps>;
}
```

- [ ] **Step 2: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: no errors (this file has no consumers yet, so it just needs to
compile standalone).

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/shell/paneTypes.ts
git commit -m "feat(dashboard): pane-plugin type contract (paneTypes.ts)"
```

---

## Task 3: Pane registry (`dashboard/src/panes/registry.ts`)

**Files:**
- Create: `dashboard/src/panes/registry.ts`
- Create: `dashboard/src/panes/__tests__/registry.test.ts`

**Interfaces:**
- Consumes: `PaneEntry`, `PaneManifest` from `@/shell/paneTypes` (Task 2).
- Produces: `PANE_REGISTRY: Record<string, PaneEntry>`, exported for the
  (future) shell and for this plan's own parity test (Task 12).

- [ ] **Step 1: Write the failing test**

```ts
// dashboard/src/panes/__tests__/registry.test.ts
import { describe, expect, it } from "vitest";
import { PANE_REGISTRY } from "../registry";

describe("PANE_REGISTRY", () => {
  it("every entry's manifest.id matches its registry key", () => {
    for (const [key, entry] of Object.entries(PANE_REGISTRY)) {
      expect(entry.manifest.id).toBe(key);
    }
  });

  it("has no duplicate open_shortcut values", () => {
    const shortcuts = Object.values(PANE_REGISTRY)
      .map((e) => e.manifest.open_shortcut)
      .filter((s): s is string => s != null);
    expect(new Set(shortcuts).size).toBe(shortcuts.length);
  });

  it("every entry has a resolvable Component", () => {
    for (const entry of Object.values(PANE_REGISTRY)) {
      expect(entry.Component).toBeTypeOf("function");
    }
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/__tests__/registry.test.ts`
Expected: FAIL — `../registry` does not exist yet.

- [ ] **Step 3: Write `dashboard/src/panes/registry.ts`**

```ts
/**
 * Static pane-view registry, assembled at build time from every
 * dashboard/src/panes/<view-id>/manifest.ts + index.tsx pair.
 *
 * Source of truth: docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md §4.1/§4.2
 *
 * Adding a new pane view: create the directory with manifest.ts + index.tsx
 * exporting `manifest` and a default component respectively — this file
 * picks it up automatically via import.meta.glob, no manual registration.
 */
import type { ComponentType } from "react";
import type { PaneEntry, PaneManifest, PaneViewProps } from "@/shell/paneTypes";

const manifestModules = import.meta.glob<{ manifest: PaneManifest }>(
  "./*/manifest.ts",
  { eager: true },
);
const componentModules = import.meta.glob<{ default: ComponentType<PaneViewProps> }>(
  "./*/index.tsx",
  { eager: true },
);

function dirIdFromPath(path: string): string {
  // "./file-browser/manifest.ts" -> "file-browser"
  const match = /^\.\/([^/]+)\//.exec(path);
  if (!match) {
    throw new Error(`pane registry: cannot derive view id from path "${path}"`);
  }
  return match[1];
}

function buildRegistry(): Record<string, PaneEntry> {
  const registry: Record<string, PaneEntry> = {};
  const seenShortcuts = new Map<string, string>();

  for (const [path, mod] of Object.entries(manifestModules)) {
    const dirId = dirIdFromPath(path);
    const manifest = mod.manifest;
    if (!manifest) {
      throw new Error(`pane registry: ${path} has no named export "manifest"`);
    }
    if (manifest.id !== dirId) {
      throw new Error(
        `pane registry: manifest.id "${manifest.id}" does not match directory "${dirId}"`,
      );
    }
    if (registry[manifest.id]) {
      throw new Error(`pane registry: duplicate view id "${manifest.id}"`);
    }

    const componentPath = `./${dirId}/index.tsx`;
    const componentMod = componentModules[componentPath];
    if (!componentMod?.default) {
      throw new Error(
        `pane registry: ${componentPath} has no default export component`,
      );
    }

    if (manifest.open_shortcut) {
      const existing = seenShortcuts.get(manifest.open_shortcut);
      if (existing) {
        throw new Error(
          `pane registry: open_shortcut "${manifest.open_shortcut}" used by ` +
            `both "${existing}" and "${manifest.id}"`,
        );
      }
      seenShortcuts.set(manifest.open_shortcut, manifest.id);
    }

    registry[manifest.id] = { manifest, Component: componentMod.default };
  }

  return registry;
}

export const PANE_REGISTRY: Record<string, PaneEntry> = buildRegistry();
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/__tests__/registry.test.ts`
Expected: PASS (registry is empty at this point — zero manifests exist
yet — so all three assertions vacuously pass over an empty object).

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/panes/registry.ts dashboard/src/panes/__tests__/registry.test.ts
git commit -m "feat(dashboard): pane view registry (import.meta.glob)"
```

---

## Task 4: Extract `serve_workspace_relative_file` shared helper

**Files:**
- Create: `src/api/file_serving.py`
- Modify: `src/api/task_files.py`
- Test: `tests/test_api_task_files.py` (existing — must pass unmodified)

**Interfaces:**
- Produces: `async def serve_workspace_relative_file(workspace_path: str, path: str) -> PlainTextResponse | JSONResponse` — path-safe, size-capped,
  binary-aware file read. Raises `fastapi.HTTPException` with the same
  status codes the current `task_files.py::get_file` raises (403/404/413).
- Consumes (by callers): a resolved `workspace_path: str` — callers own
  resolving "task → workspace" or "workspace_id → workspace" before calling
  this.

- [ ] **Step 1: Run the existing test suite to capture the baseline**

Run: `pytest tests/test_api_task_files.py -v`
Expected: all tests PASS (this is the regression baseline — re-run at the
end of this task with zero diffs in outcome).

- [ ] **Step 2: Write `src/api/file_serving.py`**

This is a lift of `task_files.py::get_file`'s body (lines 165–242) with
`task_id`/`ws` replaced by a `workspace_path: str` parameter — identical
logic, no behavior change.

```python
"""Shared, path-safe, size-capped, binary-aware file-read helper.

Used by both ``/api/tasks/{id}/file`` (src/api/task_files.py) and
``/api/workspaces/{id}/file`` (src/api/workspace_files.py) — both resolve
to "a workspace root + a relative path" by the time they call this.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

__all__ = ["serve_workspace_relative_file", "MAX_FILE_BYTES"]

MAX_FILE_BYTES = 512 * 1024  # 512 KB


async def serve_workspace_relative_file(
    workspace_path: str, path: str
) -> PlainTextResponse | JSONResponse:
    """Path-safe, size-capped, binary-aware file read.

    Raises ``HTTPException`` for every rejection case:
    - 403: absolute path, ``..`` traversal, or symlink escape.
    - 404: target missing, not a regular file, or unreadable.
    - 413: target exceeds ``MAX_FILE_BYTES``.
    """
    # ── Path safety ────────────────────────────────────────────────
    # Reject absolute paths outright — an absolute ``path`` would cause
    # ``root / path`` to discard ``root`` and jump anywhere.
    if Path(path).is_absolute():
        raise HTTPException(status_code=403, detail="absolute path not allowed")

    # Resolve BOTH sides, then verify containment. We must resolve before
    # comparing so that symlink escapes and ``..`` segments both collapse
    # to their real target. ``strict=True`` on the file path turns a
    # missing file into a FileNotFoundError we can map to 404.
    root = Path(workspace_path).resolve()

    # First pass: non-strict resolve of the *lexical* path so ``..``
    # segments collapse without touching the filesystem. This catches
    # traversal even when the target doesn't exist, so a missing
    # ``../secret`` is a 403 (escape attempt) not a 404.
    lexical = (root / path).resolve()
    try:
        lexical.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="path escapes workspace")

    # Second pass: strict resolve to follow symlinks and error on missing
    # files. A symlink whose real target lies outside the workspace is a
    # 403.
    try:
        candidate = (root / path).resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="file not found")
    except (OSError, RuntimeError):
        # RuntimeError: symlink loop. OSError: permission etc.
        raise HTTPException(status_code=403, detail="path not accessible")

    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="path escapes workspace")

    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="not a regular file")

    try:
        size = candidate.stat().st_size
    except OSError:
        raise HTTPException(status_code=404, detail="file not stat-able")
    if size > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds {MAX_FILE_BYTES} byte cap",
        )

    try:
        data = candidate.read_bytes()
    except OSError as e:
        raise HTTPException(status_code=404, detail=f"read failed: {e}")

    # Binary heuristic: any NUL byte in the first 8 KiB → treat as binary.
    if b"\0" in data[:8192]:
        try:
            relative = str(candidate.relative_to(root))
        except ValueError:
            relative = path
        return JSONResponse(
            content={
                "success": True,
                "reason": "binary",
                "size": len(data),
                "path": relative,
            }
        )

    text = data.decode("utf-8", errors="replace")
    return PlainTextResponse(content=text, media_type="text/plain; charset=utf-8")
```

- [ ] **Step 3: Rewrite `task_files.py::get_file` as a thin wrapper**

In `src/api/task_files.py`:

Replace the import block:

```python
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from src.api import dependencies as deps
```

with:

```python
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from src.api import dependencies as deps
from src.api.file_serving import serve_workspace_relative_file
```

Remove the module-level `MAX_FILE_BYTES = 512 * 1024  # 512 KB` constant
(now lives in `file_serving.py`; nothing else in this file reads it after
this change).

Replace the entire body of `get_file` (everything from
`@router.get("/api/tasks/{task_id}/file")` through the function's closing
line, i.e. current lines 151–242) with:

```python
    @router.get("/api/tasks/{task_id}/file")
    async def get_file(task_id: str, path: str = Query(...)):
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")

        task = await orch.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"No task '{task_id}'")

        ws = await orch.db.get_workspace_for_task(task_id)
        if ws is None or not ws.workspace_path:
            raise HTTPException(status_code=404, detail="task has no workspace")

        return await serve_workspace_relative_file(ws.workspace_path, path)
```

Note `JSONResponse`/`PlainTextResponse` imports stay in `task_files.py`
even though this function no longer constructs them directly — they're
still used as the route's implicit return-type annotations are inferred
from `serve_workspace_relative_file`'s own signature, and removing the
import is not required; leave them for readability/consistency with the
module's docstring. (If `ruff` flags them unused after this edit, drop
them — but keep `HTTPException`, which the 503/404 branches above still
raise directly.)

- [ ] **Step 4: Run the regression suite**

Run: `pytest tests/test_api_task_files.py -v`
Expected: all tests PASS, identical to Step 1's baseline, with zero test
file changes (`tests/test_api_task_files.py` is untouched by this task).

- [ ] **Step 5: Commit**

```bash
git add src/api/file_serving.py src/api/task_files.py
git commit -m "refactor(api): extract serve_workspace_relative_file from task_files.get_file"
```

---

## Task 5: `src/api/workspace_files.py` — browse + file endpoints

**Files:**
- Create: `src/api/workspace_files.py`
- Modify: `src/api/app.py`
- Test: `tests/test_workspace_files_api.py` (new)

**Interfaces:**
- Consumes: `serve_workspace_relative_file(workspace_path: str, path: str)`
  from Task 4's `src/api/file_serving.py`; `db.get_workspace(workspace_id: str) -> Workspace | None` (`src/database/queries/workspace_queries.py`);
  `RequestScope` / `LOCAL_SCOPE` from `src/api/auth.py`.
- Produces: `build_workspace_files_router(*, db) -> APIRouter` (mirrors
  `build_graph_router(*, db)`'s factory pattern in `src/api/graph.py`) and
  a module-level `router = build_workspace_files_router(db=...)`... actually
  per the daemon-wide `deps._orchestrator` pattern every other Phase-5/6
  router in this codebase uses (`task_files.py`, look at how `_orchestrator`
  is read inside the handler, not injected via the factory), this router
  reads `db` from `deps._orchestrator.db` at request time — see Step 2 for
  why the factory still takes `db` as an optional test-seam parameter while
  the default module-level `router` ignores it and goes through `deps`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workspace_files_api.py
"""Tests for /api/workspaces/{workspace_id}/browse and .../file."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api import dependencies as deps
from src.api.auth import RequestScope
from src.api.workspace_files import build_workspace_files_router
from src.database import Database
from src.models import Project, RepoSourceType, Workspace


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("# hello\n")
    (root / "sub").mkdir()
    (root / "sub" / "nested.py").write_text("print(1)\n")
    return root


@pytest.fixture
async def wired(tmp_path, repo):
    db = Database(str(tmp_path / "aq.db"))
    await db.initialize()
    await db.create_project(Project(id="proj", name="P", repo_default_branch="main"))
    await db.create_project(Project(id="other", name="O", repo_default_branch="main"))
    await db.create_workspace(Workspace(
        id="ws1", project_id="proj", workspace_path=str(repo),
        source_type=RepoSourceType.CLONE, name="main",
    ))
    await db.create_workspace(Workspace(
        id="ws-no-path", project_id="proj", workspace_path="",
        source_type=RepoSourceType.CLONE, name="empty",
    ))

    orch = MagicMock()
    orch.db = db
    orch.config = MagicMock()

    app = FastAPI()
    app.include_router(build_workspace_files_router(db=db))
    prev_orch = deps._orchestrator
    deps._orchestrator = orch

    def _client(scope: RequestScope | None = None) -> AsyncClient:
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://t")
        if scope is not None:
            # TokenAuthMiddleware isn't mounted on this bare test app —
            # request.state.scope is set by a tiny ASGI shim instead, since
            # httpx.ASGITransport calls the app directly with no middleware
            # stack. We emulate it by monkeypatching a dependency override.
            app.dependency_overrides[deps.get_request_scope] = lambda: scope
        return client

    try:
        yield _client, db, repo
    finally:
        deps._orchestrator = prev_orch
        app.dependency_overrides.clear()
        await db.close()


async def test_browse_root_lists_entries(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/browse")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["path"] == ""
    names = [e["name"] for e in body["entries"]]
    assert names == ["sub", "README.md"]  # dirs first, alphabetical within group
    readme = next(e for e in body["entries"] if e["name"] == "README.md")
    assert readme["type"] == "file"
    assert readme["size"] == 8
    sub = next(e for e in body["entries"] if e["name"] == "sub")
    assert sub["type"] == "dir"
    assert "size" not in sub


async def test_browse_subdir(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/browse", params={"path": "sub"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "sub"
    assert [e["name"] for e in body["entries"]] == ["nested.py"]


async def test_browse_rejects_traversal(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/browse", params={"path": "../../etc"})
    assert r.status_code == 403


async def test_browse_rejects_symlink_escape(wired, tmp_path):
    client_factory, _, repo = wired
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, repo / "escape")
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/browse", params={"path": "escape"})
    assert r.status_code == 403


async def test_browse_path_is_a_file_is_404(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/browse", params={"path": "README.md"})
    assert r.status_code == 404


async def test_browse_unknown_workspace_is_404(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/does-not-exist/browse")
    assert r.status_code == 404


async def test_browse_no_workspace_path(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws-no-path/browse")
    assert r.status_code == 200
    body = r.json()
    assert body["entries"] == []
    assert body["reason"] == "no_workspace_path"


async def test_browse_out_of_scope_project_is_404(wired):
    client_factory, _, _ = wired
    scope = RequestScope(kind="session", project_id="other")
    async with client_factory(scope) as ac:
        r = await ac.get("/api/workspaces/ws1/browse")
    assert r.status_code == 404


async def test_browse_global_admin_scope_succeeds(wired):
    client_factory, _, _ = wired
    scope = RequestScope(kind="session", project_id=None, elevated=True)
    async with client_factory(scope) as ac:
        r = await ac.get("/api/workspaces/ws1/browse")
    assert r.status_code == 200


async def test_file_returns_content(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/file", params={"path": "README.md"})
    assert r.status_code == 200
    assert r.text == "# hello\n"


async def test_file_binary_returns_json_reason(wired, tmp_path):
    client_factory, _, repo = wired
    (repo / "logo.png").write_bytes(b"\x89PNG\x00\x00\x00")
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/file", params={"path": "logo.png"})
    assert r.status_code == 200
    assert r.json()["reason"] == "binary"


async def test_file_size_cap(wired, tmp_path):
    client_factory, _, repo = wired
    (repo / "big.bin").write_bytes(b"x" * (512 * 1024 + 1))
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/file", params={"path": "big.bin"})
    assert r.status_code == 413


async def test_file_rejects_absolute_path(wired):
    client_factory, _, _ = wired
    async with client_factory() as ac:
        r = await ac.get("/api/workspaces/ws1/file", params={"path": "/etc/passwd"})
    assert r.status_code == 403


async def test_file_out_of_scope_project_is_404(wired):
    client_factory, _, _ = wired
    scope = RequestScope(kind="session", project_id="other")
    async with client_factory(scope) as ac:
        r = await ac.get("/api/workspaces/ws1/file", params={"path": "README.md"})
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_workspace_files_api.py -v`
Expected: FAIL — `src.api.workspace_files` module does not exist, and
`deps.get_request_scope` does not exist yet either.

- [ ] **Step 3: Add `get_request_scope` dependency to `src/api/dependencies.py`**

The existing routers each do `getattr(request.state, "scope", LOCAL_SCOPE)`
inline (`src/api/messages.py`); this router needs the same value but as an
overridable FastAPI dependency so the test fixture above can swap it in
without standing up the full `TokenAuthMiddleware` stack. Add to
`src/api/dependencies.py`, after the existing `get_orchestrator` function:

```python
def get_request_scope(request: "Request") -> "RequestScope":
    """FastAPI dependency wrapping ``request.state.scope``.

    ``TokenAuthMiddleware`` sets ``request.state.scope`` on every request
    that passes through it; router-level tests that build a bare
    ``FastAPI()`` app with no middleware stack (see
    ``tests/test_workspace_files_api.py``) override this dependency
    directly instead. Falls back to ``LOCAL_SCOPE`` (unrestricted) when no
    middleware ran, matching every other router's inline
    ``getattr(request.state, "scope", LOCAL_SCOPE)`` pattern.
    """
    from src.api.auth import LOCAL_SCOPE

    return getattr(request.state, "scope", LOCAL_SCOPE)
```

Add the needed `TYPE_CHECKING` imports at the top of the file:

```python
if TYPE_CHECKING:
    from fastapi import Request

    from src.api.auth import RequestScope
    from src.api.auth import SessionTokenStore
    from src.commands.handler import CommandHandler
    from src.orchestrator import Orchestrator
```

(This replaces the existing narrower `TYPE_CHECKING` block — add the two
new lines, keep the three existing ones.)

- [ ] **Step 4: Write `src/api/workspace_files.py`**

```python
"""Workspace-scoped file browsing endpoints (pane view: file-browser).

Two endpoints:

* ``GET /api/workspaces/{workspace_id}/browse?path=<relpath>`` — directory
  listing at ``path`` (default: workspace root).
* ``GET /api/workspaces/{workspace_id}/file?path=<relpath>`` — raw file
  content, delegating to the same path-safe helper
  ``/api/tasks/{id}/file`` uses (``src/api/file_serving.py``).

Unlike the task-files pair, these resolve a workspace directly by id
rather than through a task's current lock — this is what the file-browser
pane view (workspace-scoped, task-independent) needs.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.api import dependencies as deps
from src.api.auth import RequestScope
from src.api.file_serving import serve_workspace_relative_file

logger = logging.getLogger(__name__)

__all__ = ["build_workspace_files_router", "router"]


def _require_workspace_scope(scope: RequestScope, workspace) -> None:
    """404 if the caller's RequestScope can't see workspace.project_id.

    404 (not 403) to avoid leaking workspace existence to a session scoped
    to a different project — matches the task-files endpoint's posture.
    """
    if scope.kind == "local":
        return
    if scope.elevated and scope.project_id is None:
        # Global admin (dashboard-shell-v2 spec §4.2): elevated + no
        # project filter — sees every workspace.
        return
    if scope.project_id == workspace.project_id:
        return
    raise HTTPException(status_code=404, detail=f"No workspace '{workspace.id}'")


def _resolve_relative_dir(root: Path, path: str) -> Path:
    """Path-safety for a directory target — same algorithm as the file
    helper's lexical + strict resolve passes, plus an is_dir() check."""
    if Path(path).is_absolute():
        raise HTTPException(status_code=403, detail="absolute path not allowed")

    lexical = (root / path).resolve()
    try:
        lexical.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="path escapes workspace")

    try:
        candidate = (root / path).resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="directory not found")
    except (OSError, RuntimeError):
        raise HTTPException(status_code=403, detail="path not accessible")

    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="path escapes workspace")

    if not candidate.is_dir():
        raise HTTPException(status_code=404, detail="not a directory")

    return candidate


def build_workspace_files_router(*, db=None) -> APIRouter:
    """Router factory — mirrors ``build_graph_router``'s pattern.

    ``db`` is accepted for test-seam symmetry with ``build_graph_router``
    but the handlers below read the live orchestrator's ``db`` via
    ``deps._orchestrator`` at request time (matching ``task_files.py``'s
    pattern), so a caller passing a fixture ``db`` here still needs
    ``deps._orchestrator.db`` wired to that same instance — see the
    ``wired`` fixture in ``tests/test_workspace_files_api.py``.
    """
    router = APIRouter()

    @router.get("/api/workspaces/{workspace_id}/browse")
    async def browse(
        workspace_id: str,
        request: Request,
        path: str = Query(""),
        scope: RequestScope = Depends(deps.get_request_scope),
    ):
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")

        workspace = await orch.db.get_workspace(workspace_id)
        if workspace is None:
            raise HTTPException(status_code=404, detail=f"No workspace '{workspace_id}'")

        _require_workspace_scope(scope, workspace)

        if not workspace.workspace_path:
            return {
                "success": True,
                "path": path,
                "entries": [],
                "reason": "no_workspace_path",
            }

        root = Path(workspace.workspace_path).resolve()
        candidate = _resolve_relative_dir(root, path)

        entries: list[dict] = []
        for dirent in candidate.iterdir():
            try:
                is_symlink = dirent.is_symlink()
                # Classify by resolved target (dir vs file); a broken
                # symlink raises here and is omitted from the listing.
                stat_result = dirent.stat()
            except OSError:
                continue
            import stat as stat_module

            if stat_module.S_ISDIR(stat_result.st_mode):
                entries.append({
                    "name": dirent.name,
                    "type": "dir",
                    "is_symlink": is_symlink,
                })
            elif stat_module.S_ISREG(stat_result.st_mode):
                entries.append({
                    "name": dirent.name,
                    "type": "file",
                    "size": stat_result.st_size,
                    "is_symlink": is_symlink,
                })
            # Other types (sockets, fifos, etc.) are omitted.

        # Non-symlink entries omit the is_symlink key entirely (spec §8.2
        # step 5 only calls it out for entries where it's true).
        for e in entries:
            if not e.pop("is_symlink", False):
                pass
            else:
                e["is_symlink"] = True

        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))

        try:
            relative = str(candidate.relative_to(root))
        except ValueError:
            relative = path
        if relative == ".":
            relative = ""

        return {"success": True, "path": relative, "entries": entries}

    @router.get("/api/workspaces/{workspace_id}/file")
    async def get_file(
        workspace_id: str,
        request: Request,
        path: str = Query(...),
        scope: RequestScope = Depends(deps.get_request_scope),
    ):
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")

        workspace = await orch.db.get_workspace(workspace_id)
        if workspace is None:
            raise HTTPException(status_code=404, detail=f"No workspace '{workspace_id}'")

        _require_workspace_scope(scope, workspace)

        if not workspace.workspace_path:
            raise HTTPException(status_code=404, detail="workspace has no path")

        return await serve_workspace_relative_file(workspace.workspace_path, path)

    return router


router = build_workspace_files_router()
```

Note on the `is_symlink` bookkeeping above: it's written as an explicit
pop/re-add pass rather than a conditional key insert at construction time
so the sort key (`e["type"]`) is computed from a stable dict shape first —
simplify this in review if a cleaner one-pass version reads better; the
required behavior is: entries where `is_symlink` is `True` carry the key,
entries where it's `False` omit it (per file-browser spec §8.2 step 5's
response example, which only shows the key on the symlinked entry).

- [ ] **Step 5: Register the router in `src/api/app.py`**

Add the import alongside the existing `task_files_router` import:

```python
from src.api.task_files import router as task_files_router
from src.api.workspace_files import router as workspace_files_router
```

Add the `include_router` call right after the task-files one (near line
113–114), so the two workspace-file-serving routers stay adjacent:

```python
    # Task file preview (Phase 5): GET /api/tasks/{id}/files + /file
    app.include_router(task_files_router)

    # Workspace file browsing (pane view: file-browser): GET
    # /api/workspaces/{id}/browse + /file
    app.include_router(workspace_files_router)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_workspace_files_api.py -v`
Expected: all PASS.

- [ ] **Step 7: Run the full backend regression suite for touched modules**

Run: `pytest tests/test_api_task_files.py tests/test_workspace_files_api.py -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/api/workspace_files.py src/api/app.py src/api/dependencies.py \
  tests/test_workspace_files_api.py
git commit -m "feat(api): workspace browse + file endpoints for file-browser pane"
```

---

## Task 6: `dashboard/src/panes/file-browser/manifest.ts` + `hooks.ts`

**Files:**
- Create: `dashboard/src/panes/file-browser/manifest.ts`
- Create: `dashboard/src/panes/file-browser/hooks.ts`
- Test: `dashboard/src/panes/file-browser/__tests__/manifest.test.ts`

**Interfaces:**
- Consumes: `PaneManifest` from `@/shell/paneTypes` (Task 2); `legacyFetch`
  from `@/api/legacy-fetch` (existing).
- Produces: `manifest: PaneManifest<FileBrowserArgs>`,
  `fileBrowserArgsSchema`, `FileBrowserArgs` type; `useWorkspaceBrowse`,
  `useWorkspaceFile` hooks; `WorkspaceBrowseEntry`, `WorkspaceBrowseResponse`
  types — consumed by Task 8's `index.tsx`.

- [ ] **Step 1: Write the failing manifest test**

```ts
// dashboard/src/panes/file-browser/__tests__/manifest.test.ts
import { describe, expect, it } from "vitest";
import { manifest, fileBrowserArgsSchema } from "../manifest";

describe("file-browser manifest", () => {
  it("id matches its directory name", () => {
    expect(manifest.id).toBe("file-browser");
  });

  it("args schema accepts workspaceId with default path", () => {
    const parsed = fileBrowserArgsSchema.parse({ workspaceId: "ws1" });
    expect(parsed).toEqual({ workspaceId: "ws1", path: "" });
  });

  it("args schema accepts workspaceId + explicit path", () => {
    const parsed = fileBrowserArgsSchema.parse({ workspaceId: "ws1", path: "a/b" });
    expect(parsed).toEqual({ workspaceId: "ws1", path: "a/b" });
  });

  it("args schema rejects missing workspaceId", () => {
    expect(() => fileBrowserArgsSchema.parse({})).toThrow();
  });

  it("args schema rejects non-string workspaceId", () => {
    expect(() => fileBrowserArgsSchema.parse({ workspaceId: 5 })).toThrow();
  });

  it("open_shortcut is a normalized $mod form", () => {
    expect(manifest.open_shortcut).toBe("$mod-shift-f");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/panes/file-browser/__tests__/manifest.test.ts`
Expected: FAIL — `../manifest` does not exist.

- [ ] **Step 3: Write `dashboard/src/panes/file-browser/manifest.ts`**

```ts
import { z } from "zod";
import { FolderOpenIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "@/shell/paneTypes";

export const fileBrowserArgsSchema = z.object({
  workspaceId: z.string().min(1),
  path: z.string().default(""),
});
export type FileBrowserArgs = z.infer<typeof fileBrowserArgsSchema>;

export const manifest: PaneManifest<FileBrowserArgs> = {
  id: "file-browser",
  name: "File Browser",
  description: "Browse files in a workspace and preview their contents.",
  icon: FolderOpenIcon,
  args_schema: fileBrowserArgsSchema,
  open_shortcut: "$mod-shift-f",
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Browse files",
  palette_section: "Workspace",
};
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/panes/file-browser/__tests__/manifest.test.ts`
Expected: PASS.

- [ ] **Step 5: Write `dashboard/src/panes/file-browser/hooks.ts`**

No test-first here — this is a thin data layer wired against the live
backend contract from Task 5; its behavior is exercised through Task 8's
component tests via mocked `fetch`.

```ts
/**
 * Data hooks for the file-browser pane view.
 *
 * Hits the workspace-scoped browse/file endpoints (src/api/workspace_files.py)
 * — not in the generated @aq/ts-client SDK (browse's entries shape and
 * file's dual text/JSON response aren't modeled there), so this goes
 * through legacyFetch, same pattern as dashboard/src/api/taskFiles.ts.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { legacyFetch } from "@/api/legacy-fetch";

export interface WorkspaceBrowseEntry {
  name: string;
  type: "dir" | "file";
  size?: number;
  is_symlink?: boolean;
}

export interface WorkspaceBrowseResponse {
  success: boolean;
  path: string;
  entries: WorkspaceBrowseEntry[];
  reason?: "no_workspace_path";
}

export interface WorkspaceFileResult {
  text: string;
  status: number;
  reason?: "binary";
  size?: number;
}

export async function fetchWorkspaceBrowse(
  workspaceId: string,
  path: string,
): Promise<WorkspaceBrowseResponse> {
  const url =
    `/api/workspaces/${encodeURIComponent(workspaceId)}/browse` +
    `?path=${encodeURIComponent(path)}`;
  const res = await legacyFetch(url);
  if (res.status === 404) {
    throw new Error("workspace not found");
  }
  if (!res.ok) throw new Error(`browse ${res.status}`);
  return (await res.json()) as WorkspaceBrowseResponse;
}

export async function fetchWorkspaceFile(
  workspaceId: string,
  path: string,
): Promise<WorkspaceFileResult> {
  const url =
    `/api/workspaces/${encodeURIComponent(workspaceId)}/file` +
    `?path=${encodeURIComponent(path)}`;
  const res = await legacyFetch(url);
  if (res.status === 413) {
    return { text: "File too large to preview (over 512 KB)", status: 413 };
  }
  if (res.status === 403) return { text: "(forbidden path)", status: 403 };
  if (res.status === 404) return { text: "(file not found)", status: 404 };
  if (!res.ok) throw new Error(`file ${res.status}`);

  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    const body = (await res.json()) as { reason?: string; size?: number };
    if (body.reason === "binary") {
      return {
        text: `(binary file, size: ${Math.round((body.size ?? 0) / 1024)} KB) — preview not available`,
        status: 200,
        reason: "binary",
        size: body.size,
      };
    }
  }
  return { text: await res.text(), status: 200 };
}

export function useWorkspaceBrowse(workspaceId: string, path: string) {
  return useQuery({
    queryKey: ["workspace-browse", workspaceId, path],
    queryFn: () => fetchWorkspaceBrowse(workspaceId, path),
    staleTime: 10_000,
  });
}

export function useWorkspaceFile(workspaceId: string, path: string | null) {
  return useQuery({
    queryKey: ["workspace-file", workspaceId, path],
    queryFn: () => fetchWorkspaceFile(workspaceId, path as string),
    enabled: path != null,
    staleTime: 10_000,
  });
}

/** Invalidation key helpers for the toolbar's Refresh action (Task 8). */
export function workspaceBrowseKey(workspaceId: string, path: string) {
  return ["workspace-browse", workspaceId, path] as const;
}
export function workspaceFileKey(workspaceId: string, path: string | null) {
  return ["workspace-file", workspaceId, path] as const;
}

export type { QueryClient } from "@tanstack/react-query" with { "resolution-mode": "import" };
```

(Drop the trailing `export type { QueryClient } ...` line if it causes a
syntax error under this repo's TS version — it's a defensive re-export for
callers that want the type without importing `@tanstack/react-query`
directly; Task 8 imports `useQueryClient` itself and doesn't need it. Judge
call for whoever implements this task: include it only if
`npm run typecheck` accepts the `with { "resolution-mode": ... }` import
attribute syntax at TS ~5.7; if not, delete the line — nothing in this
plan's other tasks depends on it.)

- [ ] **Step 6: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add dashboard/src/panes/file-browser/manifest.ts \
  dashboard/src/panes/file-browser/hooks.ts \
  dashboard/src/panes/file-browser/__tests__/manifest.test.ts
git commit -m "feat(dashboard): file-browser pane manifest + data hooks"
```

---

## Task 7: Shared file-preview rendering (`FilePreviewBody`)

**Files:**
- Create: `dashboard/src/components/FilePreviewBody.tsx`
- Test: `dashboard/src/components/__tests__/FilePreviewBody.test.tsx`

**Interfaces:**
- Produces: `<FilePreviewBody text={string} status={number} filename={string} isBinary={boolean} />`
  — extracted from `TaskFilesPanel.tsx`'s inline markdown/plain switching
  (`dashboard/src/components/TaskFilesPanel.tsx` lines 76, 114–120) so
  Task 8's file-browser preview pane and the existing task-files sidebar
  share one implementation, per file-browser spec §12's "consider
  extracting a shared preview component if it's clean."
- Consumes: `MarkdownPreview` (existing, `dashboard/src/components/MarkdownPreview.tsx`).

- [ ] **Step 1: Write the failing test**

```tsx
// dashboard/src/components/__tests__/FilePreviewBody.test.tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import FilePreviewBody from "../FilePreviewBody";

describe("FilePreviewBody", () => {
  it("renders markdown for .md files", () => {
    render(
      <FilePreviewBody filename="README.md" text="# Hello" status={200} isBinary={false} />,
    );
    expect(screen.getByRole("heading", { name: "Hello" })).toBeInTheDocument();
  });

  it("renders plain monospace for non-.md files", () => {
    render(
      <FilePreviewBody filename="app.py" text="print(1)" status={200} isBinary={false} />,
    );
    const pre = screen.getByText("print(1)");
    expect(pre.tagName).toBe("PRE");
  });

  it("renders binary placeholder text as-is (not markdown-parsed)", () => {
    render(
      <FilePreviewBody
        filename="logo.png"
        text="(binary file, size: 4 KB) — preview not available"
        status={200}
        isBinary
      />,
    );
    expect(
      screen.getByText("(binary file, size: 4 KB) — preview not available"),
    ).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd dashboard && npx vitest run src/components/__tests__/FilePreviewBody.test.tsx`
Expected: FAIL — `../FilePreviewBody` does not exist.

- [ ] **Step 3: Write `dashboard/src/components/FilePreviewBody.tsx`**

```tsx
/**
 * Shared file-content rendering: markdown for .md files, monospace <pre>
 * for everything else, with a distinct rendering for binary placeholders.
 *
 * Extracted from TaskFilesPanel's inline switch (spec:
 * docs/superpowers/specs/2026-08-22-pane-file-browser-design.md §12) so
 * the task-files sidebar and the file-browser pane share one
 * implementation.
 */
import MarkdownPreview from "./MarkdownPreview";

interface FilePreviewBodyProps {
  filename: string;
  text: string;
  status: number;
  isBinary: boolean;
}

export default function FilePreviewBody({ filename, text, isBinary }: FilePreviewBodyProps) {
  const isMd = !isBinary && filename.toLowerCase().endsWith(".md");

  if (isMd) {
    return <MarkdownPreview source={text} />;
  }

  return (
    <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap font-mono text-xs text-gray-200">
      {text}
    </pre>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd dashboard && npx vitest run src/components/__tests__/FilePreviewBody.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/components/FilePreviewBody.tsx \
  dashboard/src/components/__tests__/FilePreviewBody.test.tsx
git commit -m "refactor(dashboard): extract FilePreviewBody from TaskFilesPanel"
```

---

## Task 8: `dashboard/src/panes/file-browser/index.tsx` — component

**Files:**
- Create: `dashboard/src/panes/file-browser/index.tsx`

**Interfaces:**
- Consumes: `FileBrowserArgs`, `manifest` (Task 6); `useWorkspaceBrowse`,
  `useWorkspaceFile`, `workspaceBrowseKey`, `workspaceFileKey`,
  `WorkspaceBrowseEntry` (Task 6's `hooks.ts`); `PaneViewProps` (Task 2's
  `paneTypes.ts`); `FilePreviewBody` (Task 7); `PaneToolbarAction`,
  `ShortcutBinding` (Task 2).
- Produces: default-exported `FileBrowserPane` component, satisfying
  `PaneViewProps<FileBrowserArgs>`, consumed by Task 3's registry via
  `import.meta.glob("./*/index.tsx")` (no manual wiring needed).

- [ ] **Step 1: Write `dashboard/src/panes/file-browser/index.tsx`**

```tsx
import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  FolderIcon,
  DocumentIcon,
  ArrowUpIcon,
  ArrowPathIcon,
  ClipboardDocumentIcon,
  HomeIcon,
} from "@heroicons/react/24/outline";
import type { PaneViewProps } from "@/shell/paneTypes";
import type { FileBrowserArgs } from "./manifest";
import {
  useWorkspaceBrowse,
  useWorkspaceFile,
  workspaceBrowseKey,
  workspaceFileKey,
  type WorkspaceBrowseEntry,
} from "./hooks";
import FilePreviewBody from "@/components/FilePreviewBody";

function parentPath(path: string): string {
  const segments = path.split("/").filter(Boolean);
  segments.pop();
  return segments.join("/");
}

function breadcrumbSegments(path: string): { label: string; path: string }[] {
  const segments = path.split("/").filter(Boolean);
  const crumbs: { label: string; path: string }[] = [{ label: "root", path: "" }];
  let acc = "";
  for (const seg of segments) {
    acc = acc ? `${acc}/${seg}` : seg;
    crumbs.push({ label: seg, path: acc });
  }
  return crumbs;
}

export default function FileBrowserPane({
  args,
  close,
  setArgs,
  setToolbar,
  setShortcuts,
}: PaneViewProps<FileBrowserArgs>) {
  const { workspaceId, path } = args;
  const queryClient = useQueryClient();

  const [previewPath, setPreviewPath] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [focusIndex, setFocusIndex] = useState(0);

  const browseQ = useWorkspaceBrowse(workspaceId, path);
  const fileQ = useWorkspaceFile(workspaceId, previewPath);

  // Mount-time / workspaceId-change file-push fallback (file-browser spec
  // §10): if `path` 404s "not a directory" — i.e. the caller pointed args
  // at a file, not a dir — retry against the parent and preview the file.
  useEffect(() => {
    if (browseQ.error && browseQ.error.message.includes("not a directory")) {
      const parent = parentPath(path);
      setArgs({ workspaceId, path: parent });
      setPreviewPath(path);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [browseQ.error]);

  // Full reset on workspaceId change (spec §5.4 / §9's edge-case table).
  const [lastWorkspaceId, setLastWorkspaceId] = useState(workspaceId);
  useEffect(() => {
    if (workspaceId !== lastWorkspaceId) {
      setPreviewPath(null);
      setFilter("");
      setFocusIndex(0);
      setLastWorkspaceId(workspaceId);
    }
  }, [workspaceId, lastWorkspaceId]);

  const entries = useMemo(() => {
    const all = browseQ.data?.entries ?? [];
    if (!filter) return all;
    const needle = filter.toLowerCase();
    return all.filter((e) => e.name.toLowerCase().includes(needle));
  }, [browseQ.data, filter]);

  function openEntry(entry: WorkspaceBrowseEntry) {
    if (entry.type === "dir") {
      const nextPath = path ? `${path}/${entry.name}` : entry.name;
      setArgs({ workspaceId, path: nextPath });
      setFilter("");
    } else {
      const nextPath = path ? `${path}/${entry.name}` : entry.name;
      setPreviewPath(nextPath);
    }
  }

  function upOneDir() {
    if (path === "") return;
    setArgs({ workspaceId, path: parentPath(path) });
  }

  function openRoot() {
    if (path === "") return;
    setArgs({ workspaceId, path: "" });
  }

  function refresh() {
    queryClient.invalidateQueries({ queryKey: workspaceBrowseKey(workspaceId, path) });
    if (previewPath != null) {
      queryClient.invalidateQueries({ queryKey: workspaceFileKey(workspaceId, previewPath) });
    }
  }

  function copyPath() {
    const target = previewPath ?? path;
    void navigator.clipboard.writeText(target);
  }

  useEffect(() => {
    setToolbar([
      { id: "refresh", label: "Refresh", icon: ArrowPathIcon, onClick: refresh },
      { id: "copy-path", label: "Copy path", icon: ClipboardDocumentIcon, onClick: copyPath },
      {
        id: "up",
        label: "Up one dir",
        icon: ArrowUpIcon,
        onClick: upOneDir,
        disabled: path === "",
      },
      {
        id: "root",
        label: "Open workspace root",
        icon: HomeIcon,
        onClick: openRoot,
        disabled: path === "",
      },
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, previewPath, workspaceId]);

  useEffect(() => {
    const bindings: import("@/shell/paneTypes").ShortcutBinding[] = [
      { key: "Backspace", label: "Up one dir", onFire: upOneDir },
      {
        key: "/",
        label: "Focus filter",
        onFire: () => document.getElementById("file-browser-filter")?.focus(),
      },
      { key: "r", label: "Refresh", onFire: refresh },
    ];
    setShortcuts(bindings);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, previewPath, workspaceId]);

  const crumbs = breadcrumbSegments(path);

  return (
    <div className="flex h-full flex-col gap-2 p-2 md:grid md:grid-cols-2 md:gap-3">
      <div className="flex min-h-0 flex-col rounded border border-gray-800 bg-gray-950">
        <div className="flex flex-wrap items-center gap-1 border-b border-gray-800 px-2 py-1 text-xs text-gray-400">
          {crumbs.map((c, i) => (
            <span key={c.path}>
              {i > 0 && <span className="mx-1 text-gray-600">/</span>}
              <button
                className="hover:text-gray-100 hover:underline"
                onClick={() => setArgs({ workspaceId, path: c.path })}
              >
                {c.label}
              </button>
            </span>
          ))}
        </div>
        <input
          id="file-browser-filter"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") setFilter("");
          }}
          placeholder="Filter files…"
          className="border-b border-gray-800 bg-gray-950 px-2 py-1 text-xs text-gray-200 outline-none"
        />
        <div className="min-h-0 flex-1 overflow-y-auto">
          {browseQ.isLoading ? (
            <p className="p-3 text-sm text-gray-500">Loading…</p>
          ) : browseQ.error ? (
            <div className="p-3 text-sm text-red-400">
              Failed to load directory.{" "}
              <button className="underline" onClick={refresh}>
                Retry
              </button>
            </div>
          ) : browseQ.data?.reason === "no_workspace_path" ? (
            <p className="p-3 text-sm text-gray-500">
              This workspace has no filesystem path yet — nothing to browse.
            </p>
          ) : entries.length === 0 && filter ? (
            <p className="p-3 text-sm text-gray-500">No files match &ldquo;{filter}&rdquo;.</p>
          ) : entries.length === 0 ? (
            <p className="p-3 text-sm text-gray-500">This directory is empty.</p>
          ) : (
            <ul className="text-xs">
              {entries.map((entry, i) => (
                <li key={entry.name}>
                  <button
                    onFocus={() => setFocusIndex(i)}
                    onClick={() => openEntry(entry)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") openEntry(entry);
                    }}
                    className="flex w-full items-center gap-2 px-3 py-1 text-left hover:bg-gray-900"
                  >
                    {entry.type === "dir" ? (
                      <FolderIcon className="h-4 w-4 text-indigo-400" />
                    ) : (
                      <DocumentIcon className="h-4 w-4 text-gray-500" />
                    )}
                    <span className="flex-1 truncate font-mono text-gray-200">
                      {entry.name}
                      {entry.is_symlink && <span className="ml-1 text-amber-400">@</span>}
                    </span>
                    {entry.type === "file" && entry.size != null && (
                      <span className="text-gray-500">{entry.size}b</span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <div className="flex min-h-0 flex-col rounded border border-gray-800 bg-gray-950 p-3">
        {previewPath == null ? (
          <p className="text-sm text-gray-500">Select a file to preview</p>
        ) : fileQ.isLoading ? (
          <>
            <p className="mb-2 text-xs text-gray-500">{previewPath}</p>
            <p className="text-sm text-gray-500">Loading…</p>
          </>
        ) : fileQ.error ? (
          <div className="text-sm text-red-400">
            Failed to load file.{" "}
            <button className="underline" onClick={refresh}>
              Retry
            </button>
          </div>
        ) : (
          <>
            <p className="mb-2 text-xs text-gray-500">{previewPath}</p>
            <div className="min-h-0 flex-1 overflow-auto">
              <FilePreviewBody
                filename={previewPath}
                text={fileQ.data?.text ?? ""}
                status={fileQ.data?.status ?? 200}
                isBinary={fileQ.data?.reason === "binary"}
              />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// `close` is part of the PaneViewProps contract (header close button owns
// it in the shell) — accepted here for type compliance even though this
// v1 layout doesn't render its own close affordance.
void close;
```

Layout note: the spec calls for a `ResizeObserver`-driven collapse below
480px rather than the CSS `md:` breakpoint used above. `md:grid-cols-2`
approximates it with zero extra code for this task's scope; if a reviewer
wants the true `ResizeObserver` behavior (pane width, not viewport width,
crossing 480px), that's a follow-up — flag it in code review rather than
blocking this task, since the shell that would report pane width doesn't
exist in this repo yet (see Prerequisites).

- [ ] **Step 2: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: no errors.

- [ ] **Step 3: Run the full pane registry test to confirm pickup**

Run: `cd dashboard && npx vitest run src/panes/__tests__/registry.test.ts`
Expected: PASS — `file-browser` now appears in `PANE_REGISTRY` via
`import.meta.glob`, and its `manifest.id` ("file-browser") matches its
directory name, satisfying the parity assertion.

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/panes/file-browser/index.tsx
git commit -m "feat(dashboard): file-browser pane component"
```

---

## Task 9: Component tests for the file-browser pane

**Files:**
- Create: `dashboard/src/panes/file-browser/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: everything from Tasks 6–8 (`manifest`, `hooks`,
  `FileBrowserPane`).

- [ ] **Step 1: Write the test file**

```tsx
// dashboard/src/panes/file-browser/__tests__/index.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import FileBrowserPane from "../index";
import type { FileBrowserArgs } from "../manifest";
import type { WorkspaceBrowseResponse, WorkspaceFileResult } from "../hooks";

function renderPane(args: FileBrowserArgs) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const close = vi.fn();
  const setArgs = vi.fn();
  const setToolbar = vi.fn();
  const setShortcuts = vi.fn();

  const utils = render(
    <QueryClientProvider client={queryClient}>
      <FileBrowserPane
        args={args}
        close={close}
        setArgs={setArgs}
        setToolbar={setToolbar}
        setShortcuts={setShortcuts}
      />
    </QueryClientProvider>,
  );

  return { ...utils, close, setArgs, setToolbar, setShortcuts, queryClient };
}

function mockFetchSequence(responses: Array<() => Response>) {
  let i = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      const make = responses[Math.min(i, responses.length - 1)];
      i += 1;
      return make();
    }),
  );
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function textResponse(text: string, status = 200): Response {
  return new Response(text, {
    status,
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
}

const browseRoot: WorkspaceBrowseResponse = {
  success: true,
  path: "",
  entries: [
    { name: "src", type: "dir" },
    { name: "README.md", type: "file", size: 12 },
  ],
};

describe("FileBrowserPane", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders tree from mocked browse response (dirs before files)", async () => {
    mockFetchSequence([() => jsonResponse(browseRoot)]);
    renderPane({ workspaceId: "ws1", path: "" });

    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());
    const names = screen.getAllByText(/^(src|README\.md)$/).map((el) => el.textContent);
    expect(names).toEqual(["src", "README.md"]);
  });

  it("directory click calls setArgs with new path, same workspaceId", async () => {
    mockFetchSequence([() => jsonResponse(browseRoot)]);
    const { setArgs } = renderPane({ workspaceId: "ws1", path: "" });

    await waitFor(() => screen.getByText("src"));
    fireEvent.click(screen.getByText("src"));

    expect(setArgs).toHaveBeenCalledWith({ workspaceId: "ws1", path: "src" });
  });

  it("file click sets previewPath without calling setArgs", async () => {
    mockFetchSequence([
      () => jsonResponse(browseRoot),
      () => textResponse("# hello"),
    ]);
    const { setArgs } = renderPane({ workspaceId: "ws1", path: "" });

    await waitFor(() => screen.getByText("README.md"));
    fireEvent.click(screen.getByText("README.md"));

    await waitFor(() => expect(screen.getByText("README.md", { selector: "p" })).toBeInTheDocument());
    expect(setArgs).not.toHaveBeenCalled();
  });

  it("close prop is accepted (type contract) without being auto-invoked", async () => {
    mockFetchSequence([() => jsonResponse(browseRoot)]);
    const { close } = renderPane({ workspaceId: "ws1", path: "" });
    await waitFor(() => screen.getByText("src"));
    expect(close).not.toHaveBeenCalled();
  });

  it("setToolbar is called with four actions on mount", async () => {
    mockFetchSequence([() => jsonResponse(browseRoot)]);
    const { setToolbar } = renderPane({ workspaceId: "ws1", path: "" });

    await waitFor(() => {
      const lastCall = setToolbar.mock.calls.at(-1);
      expect(lastCall?.[0]).toHaveLength(4);
      const ids = lastCall?.[0].map((a: { id: string }) => a.id);
      expect(ids).toEqual(["refresh", "copy-path", "up", "root"]);
    });
  });

  it("Up one dir and Open workspace root are disabled at path=''", async () => {
    mockFetchSequence([() => jsonResponse(browseRoot)]);
    const { setToolbar } = renderPane({ workspaceId: "ws1", path: "" });

    await waitFor(() => {
      const lastCall = setToolbar.mock.calls.at(-1);
      const up = lastCall?.[0].find((a: { id: string }) => a.id === "up");
      const root = lastCall?.[0].find((a: { id: string }) => a.id === "root");
      expect(up?.disabled).toBe(true);
      expect(root?.disabled).toBe(true);
    });
  });

  it("setShortcuts is called with Backspace, /, r", async () => {
    mockFetchSequence([() => jsonResponse(browseRoot)]);
    const { setShortcuts } = renderPane({ workspaceId: "ws1", path: "" });

    await waitFor(() => {
      const lastCall = setShortcuts.mock.calls.at(-1);
      const keys = lastCall?.[0].map((b: { key: string }) => b.key);
      expect(keys).toEqual(["Backspace", "/", "r"]);
    });
  });

  it("empty directory shows the empty-state message", async () => {
    mockFetchSequence([
      () => jsonResponse({ success: true, path: "empty", entries: [] }),
    ]);
    renderPane({ workspaceId: "ws1", path: "empty" });

    await waitFor(() =>
      expect(screen.getByText("This directory is empty.")).toBeInTheDocument(),
    );
  });

  it("reason: no_workspace_path renders the correct message without crashing", async () => {
    mockFetchSequence([
      () =>
        jsonResponse({
          success: true,
          path: "",
          entries: [],
          reason: "no_workspace_path",
        }),
    ]);
    renderPane({ workspaceId: "ws-empty", path: "" });

    await waitFor(() =>
      expect(
        screen.getByText(
          "This workspace has no filesystem path yet — nothing to browse.",
        ),
      ).toBeInTheDocument(),
    );
  });

  it("binary file renders the binary placeholder instead of raw content", async () => {
    mockFetchSequence([
      () =>
        jsonResponse({
          success: true,
          path: "",
          entries: [{ name: "logo.png", type: "file", size: 4096 }],
        }),
      () => jsonResponse({ success: true, reason: "binary", size: 4096, path: "logo.png" }),
    ]);
    renderPane({ workspaceId: "ws1", path: "" });

    await waitFor(() => screen.getByText("logo.png"));
    fireEvent.click(screen.getByText("logo.png"));

    await waitFor(() =>
      expect(
        screen.getByText("(binary file, size: 4 KB) — preview not available"),
      ).toBeInTheDocument(),
    );
  });

  it("filter hides non-matching rows client-side with no new network call", async () => {
    mockFetchSequence([() => jsonResponse(browseRoot)]);
    const fetchSpy = vi.fn(async () => jsonResponse(browseRoot));
    vi.stubGlobal("fetch", fetchSpy);
    renderPane({ workspaceId: "ws1", path: "" });

    await waitFor(() => screen.getByText("src"));
    const callCountAfterLoad = fetchSpy.mock.calls.length;

    fireEvent.change(screen.getByPlaceholderText("Filter files…"), {
      target: { value: "readme" },
    });

    await waitFor(() => {
      expect(screen.queryByText("src")).not.toBeInTheDocument();
      expect(screen.getByText("README.md")).toBeInTheDocument();
    });
    expect(fetchSpy.mock.calls.length).toBe(callCountAfterLoad);
  });

  it("mount-time file-push fallback: browse 404s not-a-directory, parent succeeds, file auto-previews", async () => {
    mockFetchSequence([
      () => jsonResponse({ detail: "not a directory" }, 404),
      () => jsonResponse({ success: true, path: "", entries: [{ name: "README.md", type: "file", size: 12 }] }),
      () => textResponse("# hello"),
    ]);
    renderPane({ workspaceId: "ws1", path: "README.md" });

    await waitFor(() =>
      expect(screen.getByText("README.md", { selector: "p" })).toBeInTheDocument(),
    );
  });
});
```

Note: `fetchWorkspaceBrowse` in `hooks.ts` (Task 6) currently throws a
generic `Error("browse ${res.status}")` for non-200/404 responses — the
404 branch throws `Error("workspace not found")`. The mount-time-fallback
test above depends on the *404 "not a directory"* case reaching
`FileBrowserPane`'s `useEffect` as `browseQ.error.message.includes("not a
directory")`. Since `fetchWorkspaceBrowse`'s current 404 branch always
throws the fixed string `"workspace not found"` regardless of the
backend's `detail`, **this test will fail until `fetchWorkspaceBrowse` is
adjusted to surface the response body's `detail` text in the thrown
error.** Fix `fetchWorkspaceBrowse` in `dashboard/src/panes/file-browser/hooks.ts`
(Task 6) before this test can pass:

```ts
export async function fetchWorkspaceBrowse(
  workspaceId: string,
  path: string,
): Promise<WorkspaceBrowseResponse> {
  const url =
    `/api/workspaces/${encodeURIComponent(workspaceId)}/browse` +
    `?path=${encodeURIComponent(path)}`;
  const res = await legacyFetch(url);
  if (res.status === 404) {
    const body = await res.json().catch(() => ({}) as { detail?: string });
    throw new Error(body.detail ?? "workspace not found");
  }
  if (!res.ok) throw new Error(`browse ${res.status}`);
  return (await res.json()) as WorkspaceBrowseResponse;
}
```

- [ ] **Step 2: Apply the `fetchWorkspaceBrowse` fix above to `hooks.ts`**

- [ ] **Step 3: Run tests to verify they pass**

Run: `cd dashboard && npx vitest run src/panes/file-browser/__tests__/index.test.tsx`
Expected: all PASS.

- [ ] **Step 4: Run the full frontend suite**

Run: `cd dashboard && npm run test`
Expected: all PASS (manifest, registry, FilePreviewBody, and index tests).

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/panes/file-browser/__tests__/index.test.tsx \
  dashboard/src/panes/file-browser/hooks.ts
git commit -m "test(dashboard): file-browser pane component tests"
```

---

## Task 10: Server-side pane registry mirror + parity test

**Files:**
- Create: `src/panes/__init__.py`
- Create: `src/panes/registry.py`
- Create: `tests/test_pane_registry_parity.py`

**Interfaces:**
- Produces: `SERVER_PANE_REGISTRY: dict[str, dict]` — plugin-interface
  spec §7 option A. `{"file-browser": {"agent_pushable": True}}` is the
  registry's first (and, in this repo today, only) entry.

- [ ] **Step 1: Write the failing parity test**

```python
# tests/test_pane_registry_parity.py
"""Frontend/backend pane-view registry parity (plugin-interface spec §7)."""
from __future__ import annotations

import re
from pathlib import Path

from src.panes.registry import SERVER_PANE_REGISTRY

DASHBOARD_PANES_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "src" / "panes"

_ID_RE = re.compile(r'id:\s*"([^"]+)"')


def _read_frontend_manifest_ids() -> set[str]:
    """Parse `id: "..."` out of every panes/*/manifest.ts.

    Deliberately a plain-text scan, not a TS/JS parser — this test only
    needs the manifest.id literal, and the frontend has no Python-callable
    TS runtime in this repo's test environment.
    """
    ids: set[str] = set()
    if not DASHBOARD_PANES_DIR.exists():
        return ids
    for manifest_path in DASHBOARD_PANES_DIR.glob("*/manifest.ts"):
        text = manifest_path.read_text()
        match = _ID_RE.search(text)
        if match:
            ids.add(match.group(1))
    return ids


def test_frontend_and_backend_registries_match():
    frontend_ids = _read_frontend_manifest_ids()
    backend_ids = set(SERVER_PANE_REGISTRY.keys())
    assert frontend_ids == backend_ids, (
        f"frontend panes {frontend_ids} != backend registry {backend_ids} "
        "— add/remove an entry in src/panes/registry.py to match"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pane_registry_parity.py -v`
Expected: FAIL — `src.panes.registry` does not exist (frontend already has
`file-browser`'s `manifest.ts` from Task 6, so `frontend_ids ==
{"file-browser"}` while the import itself errors first).

- [ ] **Step 3: Create `src/panes/__init__.py`** (empty file, makes `src.panes` a package)

- [ ] **Step 4: Write `src/panes/registry.py`**

```python
"""Server-side mirror of the frontend pane-view registry.

Plugin-interface spec §7 option A: a hand-maintained static list, kept in
sync with dashboard/src/panes/*/manifest.ts by convention + the parity
test (tests/test_pane_registry_parity.py). Used by ``aq message send
--pane-open`` (_cmd_message_send) to validate a pushed view id exists and
is agent-pushable before accepting the frame.
"""
from __future__ import annotations

SERVER_PANE_REGISTRY: dict[str, dict] = {
    "file-browser": {"agent_pushable": True},
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_pane_registry_parity.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/panes/__init__.py src/panes/registry.py tests/test_pane_registry_parity.py
git commit -m "feat(api): server-side pane registry mirror + parity test (file-browser)"
```

---

## Task 11: Manual verification checklist

Not automated — run these by hand against a local daemon + dashboard dev
server before considering the feature done.

**Backend:**

- [ ] Start the daemon (`./run.sh start`) against a project with a
      workspace that has a real `workspace_path` (an existing project
      checkout works).
- [ ] `curl http://127.0.0.1:8081/api/workspaces/<real-ws-id>/browse` —
      confirm `{"success": true, "path": "", "entries": [...]}` with dirs
      listed before files, alphabetical within each group.
- [ ] `curl "http://127.0.0.1:8081/api/workspaces/<real-ws-id>/browse?path=src"` —
      confirm subdirectory listing.
- [ ] `curl "http://127.0.0.1:8081/api/workspaces/<real-ws-id>/browse?path=../../etc"` —
      confirm `403`.
- [ ] `curl "http://127.0.0.1:8081/api/workspaces/<real-ws-id>/file?path=README.md"` —
      confirm raw text content, `content-type: text/plain`.
- [ ] `curl http://127.0.0.1:8081/api/workspaces/does-not-exist/browse` —
      confirm `404`.
- [ ] Re-run `pytest tests/test_api_task_files.py -v` one more time after
      all backend tasks land — confirm the pre-existing task-file endpoint
      tests are still green (this is the refactor's regression gate).

**Frontend (component-level, since the shell isn't mounted yet):**

- [ ] `cd dashboard && npm run build` — confirm the new pane directory and
      `@` alias compile cleanly in a production build, not just dev/test.
- [ ] `cd dashboard && npm run lint` — confirm no new lint errors in
      `dashboard/src/panes/file-browser/`, `dashboard/src/shell/`, or
      `dashboard/src/components/FilePreviewBody.tsx`.
- [ ] `cd dashboard && npm run test` — full Vitest suite green
      (`manifest.test.ts`, `registry.test.ts`, `FilePreviewBody.test.tsx`,
      `index.test.tsx`).
- [ ] Sanity-mount `FileBrowserPane` in a scratch route or Storybook-less
      throwaway page (e.g. temporarily render it inside `App.tsx` behind a
      `?debug=file-browser` flag, or via `npm run dev` + a one-off test
      harness component) pointed at a real `workspaceId` from a running
      daemon, and manually click through: navigate into a subdirectory,
      preview a `.md` file (confirm it renders as markdown), preview a
      non-`.md` file (confirm monospace `<pre>`), click `Up one dir`,
      click a breadcrumb segment, type into the filter box. Remove the
      throwaway harness before merging — it's for this manual check only,
      not a shipped route.
- [ ] Confirm `Copy path` actually calls `navigator.clipboard.writeText`
      (check via browser devtools clipboard permission prompt or a
      `console.log` temporarily added and removed).

---

## Self-Review Notes

- **Spec coverage:** File-browser spec §3 (manifest) → Task 6. §4 (args) →
  Task 6. §5 (component/layout/tree/preview/setArgs split/filter) → Task
  8. §6 (toolbar/shortcuts) → Task 8. §7 (data/queries) → Task 6. §8
  (backend: browse, file, scope, extraction) → Tasks 4–5. §9 (loading/error
  states) → Task 8's conditional rendering + Task 9's tests. §10
  (agent-push fallback) → Task 8's `useEffect` + Task 9's dedicated test.
  §11 (test list) → Tasks 6, 9. §12 (checklist) → mapped 1:1 across Tasks
  6, 8, 9, 10. Plugin-interface spec §4 (manifest shape), §5 (component
  contract), §7 (server mirror) → Tasks 2, 6, 10. Dashboard-shell-v2 spec's
  only direct touch here is §4.2's global-admin scope shape, consumed by
  Task 5's `_require_workspace_scope`.
- **Deliberate scope trim vs. the shell spec:** `ResizeObserver`-driven
  480px collapse (file-browser spec §5.1) is approximated with a CSS
  breakpoint in Task 8 because no shell exists yet to report pane width —
  called out inline as a follow-up, not silently dropped.
- **Deviations from the file-browser spec's literal manifest example:**
  icon import is `@heroicons/react/24/outline` (`FolderOpenIcon`), not
  `lucide-react`'s `FolderTree` — the plugin-interface spec explicitly
  overrides the file-browser spec's own example on this point (see Global
  Constraints).
