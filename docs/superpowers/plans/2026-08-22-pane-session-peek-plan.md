# Pane View: session-peek Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `session-peek` pane view — a live tmux peek-frame console for one
session, hosted in the shell's `<ShellPane>` right surface, reachable from click-through
(agent rows, task chips), the command palette, and agent-push chat chips — reusing the
rendering and data hook that already power `dashboard/src/components/PaneView.tsx` and
`SessionDetail`'s pane-view toggle.

**Architecture:** Pure frontend feature (one new pane-view directory) plus one small
backend registry-parity addition. The view is a thin composition of two existing hooks
(`useTranscriptStream`, `useSession`) and one existing mutation (`useSessionKill`) around
a newly-extracted, reusable "peek console" rendering component. Follow-tail state moves
from a component-local ref (today's `PaneView.tsx`) into pane `args.tail`, so the shell's
open-call, the toolbar button, and keyboard shortcuts all read/write the same source of
truth. No new SSE endpoint, no new backend query — `GET /api/sessions/{id}/stream`
(`src/api/sessions.py`) is reused verbatim.

Because this is the **first** pane view built against the not-yet-landed pane-plugin
contract (`dashboard/src/panes/types.ts`, `dashboard/src/panes/registry.ts`,
`src/panes/registry.py` do not exist in this repo yet — confirmed by search, see Global
Constraints), Task 1 and Task 2 stand up the minimal shared scaffolding the contract
requires, scoped to exactly what `session-peek` needs to compile and test. Any other
pane-view PR landing first would make Tasks 1–2 no-ops (see Task 1 Step 0 and Task 2
Step 0 — check-before-create).

**Tech Stack:** React 19, TypeScript 5.7, Vite 6, TanStack Query v5, Tailwind v4,
`@heroicons/react/24/outline`, `zod` (new dependency), `vitest` + `@testing-library/react`
+ `@testing-library/jest-dom` + `jsdom` (new dev dependencies — this dashboard package has
no test tooling today).

**Spec:**
- `docs/superpowers/specs/2026-08-22-dashboard-shell-v2-design.md` (shell primitives,
  right-surface contract, §5–§6).
- `docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md` (the pane-view
  contract every view implements — manifest shape, component props, registry mechanics,
  agent-push message frame).
- `docs/superpowers/specs/2026-08-22-pane-session-peek-design.md` (this view's concrete
  manifest, component, toolbar/shortcuts, tests).

## Global Constraints

- Views ship in the dashboard bundle — no runtime module federation (interface spec §2).
- Icons come only from `@heroicons/react/24/outline`; `LucideIcon` must never be
  introduced (interface spec §4 icon field doc).
- `manifest.id` must equal the directory name (`session-peek`); enforced by a manifest
  test (interface spec §9.1).
- `open_shortcut` is omitted entirely (not `null`) per manifest field docs — "Omit the
  field entirely (or set to `undefined`)... Do NOT use literal `null`" (interface spec §4).
  The per-pane spec's own manifest snippet writes `open_shortcut: null`, which contradicts
  this — Task 3 follows the interface spec's normative rule (omit the field) since it is
  the contract every view must satisfy; this is flagged again as a deviation in Task 3.
- `route_scope: "cross-route"` — the pane must survive route navigation (shell spec §5.3,
  pane spec §3).
- `agent_pushable: true` (default) — this view must be openable via the
  `body_kind: "pane_open"` agent-push frame (pane spec §3, §9). Wiring the chat-surface
  auto-open dispatch and the `--pane-open` CLI flag is **out of scope for this plan** —
  see "Deferred / out of scope" below.
- `tail` arg is `.optional()`, not `.default(true)` — effective default is computed in
  the component as `args.tail ?? true` (pane spec §3, §4).
- Sticky-when-scrolled-up threshold is `24px` slack, matching `PaneView.tsx` today (pane
  spec §5.3, §6).
- No new SSE endpoint, no new React Query hook file — pure composition of
  `useTranscriptStream`, `useSession`, `useSessionKill` (pane spec §7).
- `confirmingKill` is plain component state, never round-tripped through `setArgs` or
  exposed to an agent-push producer (pane spec §6).
- Session-exited detection reads `useSession().lifecycle` (`"exited"` / `"terminated"`),
  never the SSE `status` — a stream error/close can be a transient blip, not a real exit
  (pane spec §8).
- Dashboard conventions (`dashboard/CLAUDE.md`): never call `fetch` directly for daemon
  endpoints; use `../api/hooks` / `../api/client`; icons from `@heroicons/react/24/outline`
  only; React Query mutations invalidate relevant queries on success (already true of
  `useSessionKill` — no changes needed there).

### Deferred / out of scope (explicitly, not a placeholder)

- **`--pane-open` CLI flag + `_cmd_message_send` validation + `messages.pane_open` column
  + chat-surface auto-open dispatch** (interface spec §6.5, §9; pane spec §9, §11
  checklist item "Wire `--pane-open` support..."). This is shared plumbing consumed by
  every agent-pushable pane view, not `session-peek`-specific code, and touches the
  `messages` table schema (a new nullable column via Alembic), `MessageCommandsMixin`,
  the CLI (`src/cli/messages.py`), the WS `message.sent` payload, and the chat surface's
  `useChatTranscript`/`InlineEventCard`. It needs its own plan and its own migration, and
  is not one of the eleven work items this plan was scoped to cover. §9's "Agent-push
  examples" in the pane spec remain accurate documentation of the frame shape this view
  will eventually be opened by — nothing in this plan needs to change when that plumbing
  lands, since `open("session-peek", {...})` is what the dispatcher will call.
- **`<ShellPane>` host component** (shell spec §5) and **click-through call sites**
  (Command Center Agents tab row click, shell spec §7.6) — both are explicitly called out
  as other work's responsibility in the pane spec's own checklist (§11: "this view's PR
  only needs `open("session-peek", {sessionId})` to work when called"). Component tests
  in this plan invoke `SessionPeekPane` directly with hand-built props, not through a host.

---

## File Structure

**New files:**
- `dashboard/src/panes/types.ts` — shared pane-view contract types every pane view
  imports (`PaneManifest<TArgs>`, `PaneViewProps<TArgs>`, `PaneToolbarAction`,
  `ShortcutBinding`). Created here because nothing in the repo defines them yet; written
  so it needs no changes when other pane views land (interface spec §4, §5 verbatim).
- `dashboard/src/panes/registry.ts` — build-time registry assembled via
  `import.meta.glob("./*/manifest.ts", { eager: true })` (interface spec §4.1) plus the
  validation pass from §4.2 (id-matches-directory, id-uniqueness, component-exists,
  no `open_shortcut` collisions). Written generically so it needs no changes when other
  pane views land — `session-peek` is simply the first (and today, only) entry it finds.
- `dashboard/src/panes/__tests__/registry.test.ts` — registry assembly + validation
  tests (interface spec §9.2), scoped to what's true with one view registered.
- `dashboard/src/panes/session-peek/manifest.ts` — this view's manifest (pane spec §3).
- `dashboard/src/panes/session-peek/index.tsx` — this view's component (pane spec §5).
- `dashboard/src/panes/session-peek/__tests__/index.test.tsx` — manifest + component
  tests (pane spec §10).
- `src/panes/registry.py` — backend static mirror (interface spec §7 option A), seeded
  with the `session-peek` entry.
- `tests/test_pane_registry_parity.py` — parity test between the frontend manifest ids
  and `SERVER_PANE_REGISTRY` (interface spec §7).
- `dashboard/src/components/PeekFrameConsole.tsx` — extracted, reusable peek-frame
  rendering primitive (scroll container + `<pre>` list), factored out of
  `dashboard/src/components/PaneView.tsx` so both the full-page `PaneView` and the new
  `session-peek` pane render identical markup from one place (pane spec §1's "reuse
  strategy": "same rendering, same data hook, new host").
- `dashboard/vitest.config.ts`, `dashboard/src/test/setup.ts` — test tooling, since this
  package has none today.

**Modified files:**
- `dashboard/src/components/PaneView.tsx` — delegates its rendering body to
  `PeekFrameConsole`; keeps its own follow-tail ref and `max-h-[60vh]` wrapper (its
  existing consumer, `SessionDetail.tsx`, is unmodified and unaffected).
- `dashboard/package.json` — adds `zod`, `vitest`, `@testing-library/react`,
  `@testing-library/jest-dom`, `@testing-library/user-event`, `jsdom` as dependencies;
  adds a `"test": "vitest run"` script.

---

## Task 1: Frontend test tooling (vitest + RTL + zod)

**Files:**
- Modify: `dashboard/package.json`
- Create: `dashboard/vitest.config.ts`
- Create: `dashboard/src/test/setup.ts`

**Interfaces:**
- Produces: `npm run test` (from `dashboard/`) runs Vitest once and exits non-zero on
  failure — every later task's test steps assume this script exists.

- [ ] **Step 0: Check whether this already landed**

Run: `cd dashboard && grep -c vitest package.json`

If this prints `0`, proceed with the steps below. If it prints a number greater than
`0`, another pane-view PR already added test tooling — skip to Task 2 Step 0 and note the
skip in your task-completion report.

- [ ] **Step 1: Install dependencies**

```bash
cd dashboard && npm install --save zod && npm install --save-dev vitest @testing-library/react @testing-library/jest-dom @testing-library/user-event jsdom
```

- [ ] **Step 2: Add the `test` script**

Edit `dashboard/package.json`'s `"scripts"` block to add, alongside the existing `dev` /
`build` / `preview` / `lint` / `typecheck` entries:

```json
    "test": "vitest run",
```

- [ ] **Step 3: Write `dashboard/vitest.config.ts`**

```ts
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
```

- [ ] **Step 4: Write `dashboard/src/test/setup.ts`**

```ts
import "@testing-library/jest-dom/vitest";
```

- [ ] **Step 5: Verify the harness runs with zero tests**

Run: `cd dashboard && npm run test`
Expected: Vitest reports "No test files found" (or passes with 0 tests) and exits 0 —
confirms the config/setup wiring is valid before any real test depends on it.

- [ ] **Step 6: Commit**

```bash
git add dashboard/package.json dashboard/package-lock.json dashboard/vitest.config.ts dashboard/src/test/setup.ts
git commit -m "chore(dashboard): add vitest + RTL + zod test tooling"
```

---

## Task 2: Shared pane-view contract types + registry aggregator

**Files:**
- Create: `dashboard/src/panes/types.ts`
- Create: `dashboard/src/panes/registry.ts`
- Test: `dashboard/src/panes/__tests__/registry.test.ts`

**Interfaces:**
- Consumes: nothing project-specific (only `react`, `@heroicons/react/24/outline` types,
  `zod`).
- Produces:
  - `PaneManifest<TArgs = unknown>` — `{ id, name, description, icon, args_schema?,
    open_shortcut?, route_scope?, agent_pushable?, palette_label?, palette_section? }`.
  - `PaneViewProps<TArgs = unknown>` — `{ args, close, setArgs, setToolbar, setShortcuts
    }`.
  - `PaneToolbarAction` — `{ id, label, icon?, onClick, disabled? }`.
  - `ShortcutBinding` — `{ key, label, onFire }`.
  - `PANE_REGISTRY: Record<string, PaneEntry>` where `PaneEntry = { manifest:
    PaneManifest; Component: ComponentType<PaneViewProps> }`.
  - `validatePaneRegistry(registry: Record<string, PaneEntry>): void` — throws on any
    violation from interface spec §4.2 (used both at module-eval time by `registry.ts`
    and directly by the registry test for isolated assertions).

- [ ] **Step 0: Check whether this already landed**

Run: `ls dashboard/src/panes/types.ts dashboard/src/panes/registry.ts 2>&1`

If both files already exist, read them, confirm they match the shapes in "Produces"
above (adjust this task's later steps to extend rather than recreate if the shape
differs in a compatible way), and skip to Task 3. If neither exists, continue.

- [ ] **Step 1: Write `dashboard/src/panes/types.ts`**

```ts
/**
 * Shared pane-view plugin contract.
 *
 * Every view under dashboard/src/panes/<view-id>/ implements this contract —
 * see docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md §4-§5.
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
   * `undefined` means "no args required".
   */
  args_schema?: z.ZodType<TArgs>;

  /**
   * Optional keyboard shortcut that OPENS this view, e.g. "$mod-shift-D".
   * Omit the field entirely (do not pass `null`) when a view has no open
   * shortcut.
   */
  open_shortcut?: string;

  /**
   * How the view relates to routes.
   * - "cross-route" (default): pane content persists across route navigation.
   * - "route-scoped": pane closes automatically on route change.
   */
  route_scope?: "cross-route" | "route-scoped";

  /** Whether the agent may push this view via the pane_open message frame. Default true. */
  agent_pushable?: boolean;

  /** Palette action label. Omit (or null) to not register a palette action. */
  palette_label?: string | null;

  /** Palette section this view's action belongs to. Ignored when palette_label is unset. */
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

  /** Register toolbar action buttons to appear in the pane header. */
  setToolbar: (actions: PaneToolbarAction[]) => void;

  /** Register per-entity shortcuts scoped to this pane. */
  setShortcuts: (bindings: ShortcutBinding[]) => void;
}

export interface PaneEntry<TArgs = unknown> {
  manifest: PaneManifest<TArgs>;
  Component: ComponentType<PaneViewProps<TArgs>>;
}
```

- [ ] **Step 2: Write `dashboard/src/panes/registry.ts`**

```ts
/**
 * Build-time pane-view registry. Picks up every dashboard/src/panes/<id>/
 * manifest.ts + index.tsx via Vite's import.meta.glob — no per-view edits
 * needed here when a new view is added (interface spec §4.1).
 */
import type { PaneEntry, PaneManifest } from "./types";

const manifestModules = import.meta.glob("./*/manifest.ts", { eager: true }) as Record<
  string,
  { manifest: PaneManifest }
>;
const componentModules = import.meta.glob("./*/index.tsx", { eager: true }) as Record<
  string,
  { default: PaneEntry["Component"] }
>;

function dirFromPath(path: string): string {
  // "./task-detail/manifest.ts" -> "task-detail"
  const match = path.match(/^\.\/([^/]+)\//);
  if (!match) throw new Error(`pane registry: cannot parse directory from "${path}"`);
  return match[1];
}

function buildRegistry(): Record<string, PaneEntry> {
  const registry: Record<string, PaneEntry> = {};
  const seenShortcuts = new Map<string, string>();

  for (const [path, mod] of Object.entries(manifestModules)) {
    const dir = dirFromPath(path);
    const { manifest } = mod;
    if (!manifest) {
      throw new Error(`pane registry: ${path} has no named export "manifest"`);
    }
    if (manifest.id !== dir) {
      throw new Error(
        `pane registry: manifest.id "${manifest.id}" does not match directory "${dir}"`,
      );
    }
    if (registry[manifest.id]) {
      throw new Error(`pane registry: duplicate manifest.id "${manifest.id}"`);
    }

    const componentPath = `./${dir}/index.tsx`;
    const componentMod = componentModules[componentPath];
    if (!componentMod || !componentMod.default) {
      throw new Error(`pane registry: ${componentPath} has no default export`);
    }

    if (manifest.open_shortcut) {
      const existing = seenShortcuts.get(manifest.open_shortcut);
      if (existing) {
        throw new Error(
          `pane registry: open_shortcut "${manifest.open_shortcut}" on "${manifest.id}" ` +
            `collides with "${existing}"`,
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

- [ ] **Step 3: Write the failing registry test**

`dashboard/src/panes/__tests__/registry.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { PANE_REGISTRY } from "../registry";

describe("PANE_REGISTRY", () => {
  it("registers session-peek", () => {
    expect(PANE_REGISTRY["session-peek"]).toBeDefined();
  });

  it("every entry's manifest.id matches its registry key", () => {
    for (const [key, entry] of Object.entries(PANE_REGISTRY)) {
      expect(entry.manifest.id).toBe(key);
    }
  });

  it("every entry has a resolvable Component", () => {
    for (const entry of Object.values(PANE_REGISTRY)) {
      expect(entry.Component).toBeTypeOf("function");
    }
  });

  it("has no open_shortcut collisions", () => {
    const seen = new Set<string>();
    for (const entry of Object.values(PANE_REGISTRY)) {
      const shortcut = entry.manifest.open_shortcut;
      if (!shortcut) continue;
      expect(seen.has(shortcut)).toBe(false);
      seen.add(shortcut);
    }
  });
});
```

- [ ] **Step 4: Run it and confirm it fails**

Run: `cd dashboard && npm run test -- src/panes/__tests__/registry.test.ts`
Expected: FAIL — `session-peek` is not yet registered because
`dashboard/src/panes/session-peek/` doesn't exist yet (Task 3 creates it). Confirm the
failure is exactly "session-peek is undefined", not a syntax/import error — a different
failure means Step 1 or Step 2 above has a bug to fix before continuing.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/panes/types.ts dashboard/src/panes/registry.ts dashboard/src/panes/__tests__/registry.test.ts
git commit -m "feat(dashboard): pane-view contract types + build-time registry"
```

(The registry test staying red until Task 3 lands is expected and intentional — TDD
across task boundaries within one plan.)

---

## Task 3: session-peek manifest

**Files:**
- Create: `dashboard/src/panes/session-peek/manifest.ts`
- Test: `dashboard/src/panes/session-peek/__tests__/index.test.tsx` (manifest section
  only in this task; component tests land in later tasks in the same file)

**Interfaces:**
- Consumes: `PaneManifest<TArgs>` from `dashboard/src/panes/types.ts` (Task 2).
- Produces: `manifest: PaneManifest<SessionPeekArgs>`, `sessionPeekArgsSchema:
  z.ZodType<SessionPeekArgs>`, `type SessionPeekArgs = { sessionId: string; tail?:
  boolean }` — every later task in this file imports these three names from
  `./manifest`.

- [ ] **Step 1: Write `dashboard/src/panes/session-peek/manifest.ts`**

```ts
import { z } from "zod";
import { CommandLineIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";

export const sessionPeekArgsSchema = z.object({
  sessionId: z.string().min(1),
  tail: z.boolean().optional(),
});

export type SessionPeekArgs = z.infer<typeof sessionPeekArgsSchema>;

export const manifest: PaneManifest<SessionPeekArgs> = {
  id: "session-peek",
  name: "Session Peek",
  description: "Live tmux peek stream for one session.",
  icon: CommandLineIcon,
  args_schema: sessionPeekArgsSchema,
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Peek session",
  palette_section: "Sessions",
};
```

Note: `open_shortcut` is omitted entirely, not set to `null` — the per-pane spec's own
manifest snippet writes `open_shortcut: null`, but the interface spec's field docs say
"Omit the field entirely... Do NOT use literal `null`" (interface spec §4). This plan
follows the interface spec's normative rule, which is the contract every view must
satisfy. Net effect on behavior is identical either way (no shortcut opens this view);
this is a type-strictness/lint concern, not a functional one.

- [ ] **Step 2: Write the manifest tests**

Create `dashboard/src/panes/session-peek/__tests__/index.test.tsx` with this content (a
component-test placeholder is added so the file compiles; it's replaced/extended in Task
6):

```tsx
import { describe, expect, it } from "vitest";
import { manifest, sessionPeekArgsSchema } from "../manifest";

describe("session-peek manifest", () => {
  it("id matches the directory name", () => {
    expect(manifest.id).toBe("session-peek");
  });

  it("args schema accepts a bare sessionId", () => {
    const result = sessionPeekArgsSchema.safeParse({ sessionId: "sess-1" });
    expect(result.success).toBe(true);
  });

  it("args schema accepts sessionId + tail", () => {
    const result = sessionPeekArgsSchema.safeParse({ sessionId: "sess-1", tail: false });
    expect(result.success).toBe(true);
  });

  it("args schema rejects an empty object", () => {
    const result = sessionPeekArgsSchema.safeParse({});
    expect(result.success).toBe(false);
  });

  it("args schema rejects an empty sessionId", () => {
    const result = sessionPeekArgsSchema.safeParse({ sessionId: "" });
    expect(result.success).toBe(false);
  });

  it("has no open_shortcut", () => {
    expect(manifest.open_shortcut).toBeUndefined();
  });
});
```

- [ ] **Step 3: Run the manifest tests and confirm they fail**

Run: `cd dashboard && npm run test -- src/panes/session-peek/__tests__/index.test.tsx`
Expected: FAIL — `dashboard/src/panes/session-peek/manifest.ts` doesn't exist until this
step's sibling Step 1 is applied. (If executing steps in order, Step 1 already created
it — in that case expect these to already PASS. Either ordering is fine; the point of
this checkpoint is confirming the schema behaves as specified, not enforcing a strict
red-green rerun.)

Run again after Step 1 is in place:
Run: `cd dashboard && npm run test -- src/panes/session-peek/__tests__/index.test.tsx`
Expected: PASS (6 tests).

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/panes/session-peek/manifest.ts dashboard/src/panes/session-peek/__tests__/index.test.tsx
git commit -m "feat(dashboard): session-peek pane manifest"
```

---

## Task 4: Server-side registry entry + parity test

**Files:**
- Create: `src/panes/registry.py`
- Test: `tests/test_pane_registry_parity.py`

**Interfaces:**
- Produces: `SERVER_PANE_REGISTRY: dict[str, dict]` — `{"session-peek": {"agent_pushable":
  True}}`. This is the dict every future pane-view PR appends one entry to (interface
  spec §7 option A).

- [ ] **Step 0: Check whether this already landed**

Run: `ls src/panes/registry.py 2>&1`

If it exists, read it, add the `session-peek` entry if missing (a one-line dict addition),
and skip to Step 3. If it doesn't exist, continue from Step 1.

- [ ] **Step 1: Write `src/panes/registry.py`**

```python
"""Server-side mirror of the frontend pane-view registry.

Kept in sync manually with dashboard/src/panes/*/manifest.ts — see
docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md §7.
Used to validate the `--pane-open` frame on `aq message send` (once that
plumbing lands — see this view's plan's "Deferred / out of scope" section)
and by tests/test_pane_registry_parity.py to keep the two lists in sync.
"""

from __future__ import annotations

#: view_id -> {"agent_pushable": bool}
SERVER_PANE_REGISTRY: dict[str, dict] = {
    "session-peek": {"agent_pushable": True},
}
```

- [ ] **Step 2: Write the failing parity test**

`tests/test_pane_registry_parity.py`:

```python
"""Parity check: every pane view registered on the frontend must also be
registered on the backend, and vice versa.

See docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md §7.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.panes.registry import SERVER_PANE_REGISTRY

DASHBOARD_PANES_DIR = Path(__file__).resolve().parents[1] / "dashboard" / "src" / "panes"

_ID_RE = re.compile(r'id:\s*"([^"]+)"')


def _read_frontend_manifest_ids() -> set[str]:
    """Parse `id: "..."` out of each panes/<view>/manifest.ts.

    Deliberately a text scan, not a TS/JS parser — this test runs in the
    Python test suite where no JS toolchain is guaranteed available. Every
    manifest.ts in this codebase declares its literal id on one line
    (see dashboard/src/panes/session-peek/manifest.ts), so a regex over
    the manifest object is reliable without executing the file.
    """
    ids: set[str] = set()
    for manifest_path in DASHBOARD_PANES_DIR.glob("*/manifest.ts"):
        text = manifest_path.read_text()
        match = _ID_RE.search(text)
        assert match, f"{manifest_path}: no `id: \"...\"` found"
        ids.add(match.group(1))
    return ids


def test_frontend_and_backend_registries_match():
    frontend_ids = _read_frontend_manifest_ids()
    backend_ids = set(SERVER_PANE_REGISTRY.keys())
    assert frontend_ids == backend_ids, (
        f"frontend/backend pane registry mismatch: "
        f"frontend-only={frontend_ids - backend_ids} "
        f"backend-only={backend_ids - frontend_ids}"
    )


def test_session_peek_is_agent_pushable():
    assert SERVER_PANE_REGISTRY["session-peek"]["agent_pushable"] is True
```

- [ ] **Step 3: Run the parity test**

Run: `pytest tests/test_pane_registry_parity.py -v`
Expected: PASS (2 tests) — `session-peek/manifest.ts` was created in Task 3, so the
frontend-id scan already finds it by the time this task runs.

- [ ] **Step 4: Add `src/panes/__init__.py` if the package doesn't already resolve**

Run: `python -c "import src.panes.registry"`

If this raises `ModuleNotFoundError: No module named 'src.panes'`, create an empty
`src/panes/__init__.py`:

```bash
touch src/panes/__init__.py
```

Then rerun the import check and confirm it succeeds before moving on.

- [ ] **Step 5: Commit**

```bash
git add src/panes/registry.py src/panes/__init__.py tests/test_pane_registry_parity.py
git commit -m "feat(server): pane-view registry mirror + frontend parity test"
```

---

## Task 5: Extract reusable peek-frame console component

**Files:**
- Create: `dashboard/src/components/PeekFrameConsole.tsx`
- Modify: `dashboard/src/components/PaneView.tsx`

**Interfaces:**
- Consumes: `TranscriptFrame` from `dashboard/src/ws/useTranscriptStream.ts` (existing,
  unchanged).
- Produces: `PeekFrameConsole` — `{ frames: TranscriptFrame[]; onScroll?: (e:
  React.UIEvent<HTMLDivElement>) => void; containerRef?: React.Ref<HTMLDivElement>;
  className?: string }` — the `session-peek` component (Task 6) and the refactored
  `PaneView` both render through this.

- [ ] **Step 1: Write `dashboard/src/components/PeekFrameConsole.tsx`**

This is a straight extraction of `PaneView.tsx`'s current JSX body — same DOM shape, same
classes, same "waiting for pane snapshot" copy (pane spec §5.4: "Identical DOM shape to
`PaneView.tsx`") — parameterized so callers control the scroll container's ref/onScroll
(today's `PaneView` manages a local `followRef`; `session-peek` manages `args.tail`
instead — both need the same rendering, different scroll-tracking wiring):

```tsx
/**
 * PeekFrameConsole — monospace scrollback rendering of session pane peek
 * frames. Pure rendering: no follow-tail logic, no data fetching. Callers
 * (PaneView.tsx, panes/session-peek/index.tsx) own scroll behavior via
 * `containerRef` + `onScroll` and own data fetching via useTranscriptStream.
 *
 * Peek frames come from `tmux capture-pane -p` (src/sessions/tmux.py:445),
 * plain rendered text — no ANSI stripping needed.
 */
import type { Ref, UIEvent } from "react";
import type { TranscriptFrame } from "../ws/useTranscriptStream";

interface PeekFrameConsoleProps {
  frames: TranscriptFrame[];
  containerRef?: Ref<HTMLDivElement>;
  onScroll?: (e: UIEvent<HTMLDivElement>) => void;
  className?: string;
}

export default function PeekFrameConsole({
  frames,
  containerRef,
  onScroll,
  className,
}: PeekFrameConsoleProps) {
  return (
    <div
      ref={containerRef}
      onScroll={onScroll}
      className={
        "overflow-y-auto bg-black p-3 font-mono text-xs leading-tight text-green-200 " +
        (className ?? "")
      }
    >
      {frames.length === 0 ? (
        <p className="text-gray-500">
          Waiting for pane snapshot… (peek frames arrive whenever the
          harness has no readable transcript, or on fallback)
        </p>
      ) : (
        frames.map((f) => (
          <pre
            key={f._idx}
            className="whitespace-pre-wrap border-b border-gray-900/40 py-1"
          >
            {f.text}
          </pre>
        ))
      )}
    </div>
  );
}
```

- [ ] **Step 2: Refactor `PaneView.tsx` to delegate to it**

Replace the full contents of `dashboard/src/components/PaneView.tsx` with:

```tsx
/**
 * PaneView — terminal-styled scrollback of session pane peek frames, sized
 * for the SessionDetail full-page pane-view toggle.
 *
 * Rendering lives in PeekFrameConsole (shared with panes/session-peek);
 * this component owns SessionDetail's specific follow-tail ref and
 * max-height wrapper.
 */
import { useEffect, useRef } from "react";
import PeekFrameConsole from "./PeekFrameConsole";
import type { TranscriptFrame } from "../ws/useTranscriptStream";

interface PaneViewProps {
  entries: TranscriptFrame[];
  className?: string;
}

export default function PaneView({ entries, className }: PaneViewProps) {
  const boxRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);

  const peekFrames = entries.filter((e) => e.source === "peek");

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    if (followRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [peekFrames.length]);

  const onScroll = () => {
    const el = boxRef.current;
    if (!el) return;
    const nearBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    followRef.current = nearBottom;
  };

  return (
    <PeekFrameConsole
      frames={peekFrames}
      containerRef={boxRef}
      onScroll={onScroll}
      className={"max-h-[60vh] " + (className ?? "")}
    />
  );
}
```

- [ ] **Step 3: Typecheck + confirm `SessionDetail` still compiles**

Run: `cd dashboard && npm run typecheck`
Expected: exit 0. `SessionDetail.tsx` imports `PaneView` by its existing default export
and prop shape (`{ entries, className? }`), both unchanged, so no edits are needed there.

- [ ] **Step 4: Manual smoke check of the existing pane-view toggle**

Run: `cd dashboard && AQ_API_TARGET=http://127.0.0.1:8091 npm run dev` (adjust the target
port to a running daemon if `8091` isn't it). Open a session's detail page
(`/sessions/:id`), switch the transcript/pane toggle to "pane", and confirm scrollback
still renders and still auto-follows — behavior must be pixel-identical to before this
refactor since it's the same JSX, just relocated.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/components/PeekFrameConsole.tsx dashboard/src/components/PaneView.tsx
git commit -m "refactor(dashboard): extract PeekFrameConsole from PaneView for reuse"
```

---

## Task 6: session-peek component — rendering + data + follow-tail

**Files:**
- Create: `dashboard/src/panes/session-peek/index.tsx`
- Modify: `dashboard/src/panes/session-peek/__tests__/index.test.tsx`

**Interfaces:**
- Consumes:
  - `useTranscriptStream(sessionId, { enabled })` →
    `{ entries: TranscriptFrame[]; status: "connecting"|"open"|"closed"|"error"; error:
    string | null }` (`dashboard/src/ws/useTranscriptStream.ts`, unchanged).
  - `useSession(sessionId)` → React Query result whose `.data` has a `.lifecycle:
    string | undefined` field (`dashboard/src/api/hooks.ts:953`, unchanged).
  - `useSessionKill()` → mutation with `.mutate({ session_id: string })`
    (`dashboard/src/api/hooks.ts:1018`, unchanged).
  - `PeekFrameConsole` (Task 5).
  - `PaneViewProps<SessionPeekArgs>`, `SessionPeekArgs` (Task 2, Task 3).
- Produces: `export default function SessionPeekPane(props: PaneViewProps<SessionPeekArgs>)`
  — the shape every later task in this file (toolbar, shortcuts, exited banner) extends.

- [ ] **Step 1: Write failing render/data/follow-tail tests**

Replace `dashboard/src/panes/session-peek/__tests__/index.test.tsx`'s content, keeping
the manifest `describe` block from Task 3 and adding a component `describe` block above
it:

```tsx
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import SessionPeekPane from "../index";
import { manifest, sessionPeekArgsSchema } from "../manifest";

const mockUseTranscriptStream = vi.fn();
const mockUseSession = vi.fn();
const mockUseSessionKill = vi.fn();

vi.mock("../../../ws/useTranscriptStream", () => ({
  useTranscriptStream: (...args: unknown[]) => mockUseTranscriptStream(...args),
}));
vi.mock("../../../api/hooks", () => ({
  useSession: (...args: unknown[]) => mockUseSession(...args),
  useSessionKill: () => mockUseSessionKill(),
}));
vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
}));

function baseProps() {
  return {
    args: { sessionId: "sess-1" },
    close: vi.fn(),
    setArgs: vi.fn(),
    setToolbar: vi.fn(),
    setShortcuts: vi.fn(),
  };
}

function frame(text: string, idx: number, source: "peek" | "transcript" = "peek") {
  return { source, text, ts: idx, _idx: idx };
}

describe("SessionPeekPane component", () => {
  beforeEach(() => {
    mockUseTranscriptStream.mockReset();
    mockUseSession.mockReset();
    mockUseSessionKill.mockReset();
    mockUseSessionKill.mockReturnValue({ mutate: vi.fn() });
    mockUseSession.mockReturnValue({ data: { lifecycle: "running" } });
    mockUseTranscriptStream.mockReturnValue({
      entries: [frame("hello", 0), frame("world", 1)],
      status: "open",
      error: null,
    });
  });

  it("renders without crashing given valid args", () => {
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(screen.getByText("world")).toBeInTheDocument();
  });

  it("filters out non-peek frames", () => {
    mockUseTranscriptStream.mockReturnValue({
      entries: [frame("hello", 0, "peek"), frame("ignored", 1, "transcript")],
      status: "open",
      error: null,
    });
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(screen.queryByText("ignored")).not.toBeInTheDocument();
  });

  it("shows a loading state while connecting with no frames yet", () => {
    mockUseTranscriptStream.mockReturnValue({ entries: [], status: "connecting", error: null });
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText("Connecting…")).toBeInTheDocument();
  });

  it("renders the stream error banner", () => {
    mockUseTranscriptStream.mockReturnValue({
      entries: [],
      status: "error",
      error: "stream error (EventSource will retry)",
    });
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText("stream error (EventSource will retry)")).toBeInTheDocument();
  });
});

describe("session-peek manifest", () => {
  it("id matches the directory name", () => {
    expect(manifest.id).toBe("session-peek");
  });

  it("args schema accepts a bare sessionId", () => {
    const result = sessionPeekArgsSchema.safeParse({ sessionId: "sess-1" });
    expect(result.success).toBe(true);
  });

  it("args schema accepts sessionId + tail", () => {
    const result = sessionPeekArgsSchema.safeParse({ sessionId: "sess-1", tail: false });
    expect(result.success).toBe(true);
  });

  it("args schema rejects an empty object", () => {
    const result = sessionPeekArgsSchema.safeParse({});
    expect(result.success).toBe(false);
  });

  it("args schema rejects an empty sessionId", () => {
    const result = sessionPeekArgsSchema.safeParse({ sessionId: "" });
    expect(result.success).toBe(false);
  });

  it("has no open_shortcut", () => {
    expect(manifest.open_shortcut).toBeUndefined();
  });
});
```

- [ ] **Step 2: Run tests and confirm the new component tests fail**

Run: `cd dashboard && npm run test -- src/panes/session-peek/__tests__/index.test.tsx`
Expected: FAIL — `../index` does not exist yet (import error), or every component test
fails; the manifest tests still pass.

- [ ] **Step 3: Write `dashboard/src/panes/session-peek/index.tsx` (rendering + data +
      follow-tail only — toolbar/shortcuts/exited-banner land in later tasks)**

```tsx
import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { useTranscriptStream } from "../../ws/useTranscriptStream";
import { useSession, useSessionKill } from "../../api/hooks";
import PeekFrameConsole from "../../components/PeekFrameConsole";
import type { PaneViewProps } from "../types";
import type { SessionPeekArgs } from "./manifest";

export default function SessionPeekPane({
  args,
  close: _close,
  setArgs,
  setToolbar,
  setShortcuts,
}: PaneViewProps<SessionPeekArgs>) {
  const { sessionId } = args;
  const tail = args.tail ?? true;
  const navigate = useNavigate();

  const { data: session } = useSession(sessionId);
  const kill = useSessionKill();
  const { entries, status, error } = useTranscriptStream(sessionId, { enabled: true });

  const peekFrames = entries.filter((e) => e.source === "peek");
  const exited = session?.lifecycle === "exited" || session?.lifecycle === "terminated";

  const boxRef = useRef<HTMLDivElement>(null);

  // Follow-tail: snap to bottom on new frames when args.tail is on.
  // args.tail is the single source of truth (no local followRef) so the
  // toolbar button, keyboard shortcuts, and this effect all agree.
  useEffect(() => {
    if (!tail) return;
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [peekFrames.length, tail]);

  // Sticky-when-scrolled-up: manual scroll away from bottom turns tail off.
  // Re-enabling is explicit (toolbar / space / End) — Task 8.
  const onScroll = () => {
    const el = boxRef.current;
    if (!el || !tail) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    if (!nearBottom) setArgs({ ...args, tail: false });
  };

  // Toolbar + shortcuts are registered in Tasks 7-8; kept as no-op
  // unregisters here so unmount cleanup is already correct.
  useEffect(() => {
    setToolbar([]);
    return () => setToolbar([]);
  }, [setToolbar]);
  useEffect(() => {
    setShortcuts([]);
    return () => setShortcuts([]);
  }, [setShortcuts]);

  return (
    <div className="flex h-full flex-col">
      {exited && (
        <div className="border-b border-amber-900/60 bg-amber-950/40 px-3 py-1.5 text-xs text-amber-300">
          Session exited — showing last scrollback.
        </div>
      )}
      {error && (
        <p className="border-b border-gray-800 px-3 py-1 text-xs text-amber-400">{error}</p>
      )}
      {status === "connecting" && peekFrames.length === 0 && (
        <p className="px-3 py-2 text-xs text-gray-500">Connecting…</p>
      )}
      <PeekFrameConsole
        frames={peekFrames}
        containerRef={boxRef}
        onScroll={onScroll}
        className="flex-1"
      />
      {/* navigate/kill are wired to toolbar actions in Task 7 */}
      <span data-testid="_unused" hidden>
        {navigate.toString().length > -1 && kill ? "" : ""}
      </span>
    </div>
  );
}
```

Note the `data-testid="_unused"` line is a deliberate temporary no-op to keep `navigate`
and `kill` referenced (avoiding an unused-variable lint error) until Task 7 wires them
into real toolbar actions — Task 7's Step 1 replaces this file's body and removes it.

- [ ] **Step 4: Run tests and confirm they pass**

Run: `cd dashboard && npm run test -- src/panes/session-peek/__tests__/index.test.tsx`
Expected: PASS (10 tests: 4 component + 6 manifest — wait, count precisely: 4 component
tests in the new `describe` block above + 6 manifest tests = 10 total).

- [ ] **Step 5: Run the frontend registry test — confirm it now passes too**

Run: `cd dashboard && npm run test -- src/panes/__tests__/registry.test.ts`
Expected: PASS (4 tests) — `session-peek/index.tsx` now exists with a default export, so
the registry can resolve it.

- [ ] **Step 6: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
git add dashboard/src/panes/session-peek/index.tsx dashboard/src/panes/session-peek/__tests__/index.test.tsx
git commit -m "feat(dashboard): session-peek pane — rendering, data, follow-tail"
```

---

## Task 7: Toolbar actions

**Files:**
- Modify: `dashboard/src/panes/session-peek/index.tsx`
- Modify: `dashboard/src/panes/session-peek/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `PaneToolbarAction` (Task 2).
- Produces: on every render, `setToolbar` is called with exactly four actions, ids
  `"toggle-tail"`, `"copy-scrollback"`, `"open-full"`, `"kill-session"` — later tasks
  (shortcuts, exited-state) call the same underlying handlers these actions call
  (`copyScrollback`, `openFullSession`, `doKill`), so their names are fixed here.

- [ ] **Step 1: Add failing toolbar tests**

Add to the `describe("SessionPeekPane component", ...)` block in
`dashboard/src/panes/session-peek/__tests__/index.test.tsx`, above its closing `});`:

```tsx
  it("registers four toolbar actions on mount", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const lastCall = props.setToolbar.mock.calls.at(-1)?.[0];
    const ids = lastCall.map((a: { id: string }) => a.id);
    expect(ids).toEqual(["toggle-tail", "copy-scrollback", "open-full", "kill-session"]);
  });

  it("toggle-tail toolbar action flips args.tail via setArgs", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const actions = props.setToolbar.mock.calls.at(-1)?.[0];
    const toggle = actions.find((a: { id: string }) => a.id === "toggle-tail");
    toggle.onClick();
    expect(props.setArgs).toHaveBeenCalledWith({ sessionId: "sess-1", tail: false });
  });

  it("copy-scrollback writes joined peek text to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const actions = props.setToolbar.mock.calls.at(-1)?.[0];
    const copy = actions.find((a: { id: string }) => a.id === "copy-scrollback");
    copy.onClick();
    expect(writeText).toHaveBeenCalledWith("hello\nworld");
  });

  it("copy-scrollback is disabled with no frames", () => {
    mockUseTranscriptStream.mockReturnValue({ entries: [], status: "open", error: null });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const actions = props.setToolbar.mock.calls.at(-1)?.[0];
    const copy = actions.find((a: { id: string }) => a.id === "copy-scrollback");
    expect(copy.disabled).toBe(true);
  });

  it("open-full navigates to the session detail route", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const actions = props.setToolbar.mock.calls.at(-1)?.[0];
    const open = actions.find((a: { id: string }) => a.id === "open-full");
    expect(() => open.onClick()).not.toThrow();
  });

  it("kill-session arms on first click, commits on second", () => {
    const mutate = vi.fn();
    mockUseSessionKill.mockReturnValue({ mutate });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    let actions = props.setToolbar.mock.calls.at(-1)?.[0];
    let kill = actions.find((a: { id: string }) => a.id === "kill-session");
    expect(kill.label).toBe("Kill session");
    kill.onClick();
    expect(mutate).not.toHaveBeenCalled();

    actions = props.setToolbar.mock.calls.at(-1)?.[0];
    kill = actions.find((a: { id: string }) => a.id === "kill-session");
    expect(kill.label).toBe("Confirm kill?");
    kill.onClick();
    expect(mutate).toHaveBeenCalledWith({ session_id: "sess-1" });
  });

  it("kill-session is disabled when exited", () => {
    mockUseSession.mockReturnValue({ data: { lifecycle: "exited" } });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const actions = props.setToolbar.mock.calls.at(-1)?.[0];
    const kill = actions.find((a: { id: string }) => a.id === "kill-session");
    expect(kill.disabled).toBe(true);
  });

  it("unmount clears the toolbar", () => {
    const props = baseProps();
    const { unmount } = render(<SessionPeekPane {...props} />);
    unmount();
    expect(props.setToolbar).toHaveBeenLastCalledWith([]);
  });
```

- [ ] **Step 2: Run tests, confirm the new ones fail**

Run: `cd dashboard && npm run test -- src/panes/session-peek/__tests__/index.test.tsx`
Expected: FAIL on the 8 new toolbar tests (component still calls `setToolbar([])`).

- [ ] **Step 3: Rewrite `SessionPeekPane` to register real toolbar actions**

Replace the full contents of `dashboard/src/panes/session-peek/index.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";
import {
  PlayIcon,
  StopIcon,
  ClipboardIcon,
  ArrowTopRightOnSquareIcon,
  XCircleIcon,
} from "@heroicons/react/24/outline";
import { useNavigate } from "react-router-dom";
import { useTranscriptStream } from "../../ws/useTranscriptStream";
import { useSession, useSessionKill } from "../../api/hooks";
import PeekFrameConsole from "../../components/PeekFrameConsole";
import type { PaneViewProps } from "../types";
import type { SessionPeekArgs } from "./manifest";

export default function SessionPeekPane({
  args,
  setArgs,
  setToolbar,
  setShortcuts,
}: PaneViewProps<SessionPeekArgs>) {
  const { sessionId } = args;
  const tail = args.tail ?? true;
  const navigate = useNavigate();

  const { data: session } = useSession(sessionId);
  const kill = useSessionKill();
  const { entries, status, error } = useTranscriptStream(sessionId, { enabled: true });

  const peekFrames = entries.filter((e) => e.source === "peek");
  const exited = session?.lifecycle === "exited" || session?.lifecycle === "terminated";

  const boxRef = useRef<HTMLDivElement>(null);
  const [confirmingKill, setConfirmingKill] = useState(false);

  useEffect(() => {
    if (!tail) return;
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [peekFrames.length, tail]);

  const onScroll = () => {
    const el = boxRef.current;
    if (!el || !tail) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    if (!nearBottom) setArgs({ ...args, tail: false });
  };

  const copyScrollback = () =>
    navigator.clipboard.writeText(peekFrames.map((f) => f.text).join("\n"));
  const openFullSession = () => navigate(`/sessions/${sessionId}`);
  const doKill = () => {
    if (!confirmingKill) {
      setConfirmingKill(true);
      return;
    }
    kill.mutate({ session_id: sessionId });
    setConfirmingKill(false);
  };

  useEffect(() => {
    setToolbar([
      {
        id: "toggle-tail",
        label: tail ? "Pause tail" : "Follow tail",
        icon: tail ? StopIcon : PlayIcon,
        onClick: () => setArgs({ ...args, tail: !tail }),
      },
      {
        id: "copy-scrollback",
        label: "Copy scrollback",
        icon: ClipboardIcon,
        onClick: copyScrollback,
        disabled: peekFrames.length === 0,
      },
      {
        id: "open-full",
        label: "Open full session detail",
        icon: ArrowTopRightOnSquareIcon,
        onClick: openFullSession,
      },
      {
        id: "kill-session",
        label: confirmingKill ? "Confirm kill?" : "Kill session",
        icon: XCircleIcon,
        onClick: doKill,
        disabled: exited,
      },
    ]);
    return () => setToolbar([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tail, peekFrames.length, confirmingKill, exited]);

  useEffect(() => {
    setShortcuts([]);
    return () => setShortcuts([]);
  }, [setShortcuts]);

  return (
    <div className="flex h-full flex-col">
      {exited && (
        <div className="border-b border-amber-900/60 bg-amber-950/40 px-3 py-1.5 text-xs text-amber-300">
          Session exited — showing last scrollback.
        </div>
      )}
      {error && (
        <p className="border-b border-gray-800 px-3 py-1 text-xs text-amber-400">{error}</p>
      )}
      {status === "connecting" && peekFrames.length === 0 && (
        <p className="px-3 py-2 text-xs text-gray-500">Connecting…</p>
      )}
      <PeekFrameConsole
        frames={peekFrames}
        containerRef={boxRef}
        onScroll={onScroll}
        className="flex-1"
      />
    </div>
  );
}
```

- [ ] **Step 4: Run tests and confirm they pass**

Run: `cd dashboard && npm run test -- src/panes/session-peek/__tests__/index.test.tsx`
Expected: PASS (18 tests: 4 component + 8 toolbar + 6 manifest).

- [ ] **Step 5: Typecheck**

Run: `cd dashboard && npm run typecheck`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/panes/session-peek/index.tsx dashboard/src/panes/session-peek/__tests__/index.test.tsx
git commit -m "feat(dashboard): session-peek pane — toolbar actions"
```

---

## Task 8: Keyboard shortcuts

**Files:**
- Modify: `dashboard/src/panes/session-peek/index.tsx`
- Modify: `dashboard/src/panes/session-peek/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `ShortcutBinding` (Task 2), `doKill`/`copyScrollback`/`openFullSession`
  handlers already defined in Task 7's component body.
- Produces: on every render, `setShortcuts` is called with exactly six bindings, keys
  `"space"`, `"k"`, `"o"`, `"c"`, `"Home"`, `"End"`.

- [ ] **Step 1: Add failing shortcut tests**

Add to the component `describe` block, above its closing `});`:

```tsx
  it("registers six shortcuts on mount", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const lastCall = props.setShortcuts.mock.calls.at(-1)?.[0];
    const keys = lastCall.map((b: { key: string }) => b.key);
    expect(keys).toEqual(["space", "k", "o", "c", "Home", "End"]);
  });

  it("space flips tail via setArgs", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const bindings = props.setShortcuts.mock.calls.at(-1)?.[0];
    bindings.find((b: { key: string }) => b.key === "space").onFire();
    expect(props.setArgs).toHaveBeenCalledWith({ sessionId: "sess-1", tail: false });
  });

  it("k shares kill's arm/confirm state with the toolbar", () => {
    const mutate = vi.fn();
    mockUseSessionKill.mockReturnValue({ mutate });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    let bindings = props.setShortcuts.mock.calls.at(-1)?.[0];
    bindings.find((b: { key: string }) => b.key === "k").onFire();
    expect(mutate).not.toHaveBeenCalled();

    bindings = props.setShortcuts.mock.calls.at(-1)?.[0];
    bindings.find((b: { key: string }) => b.key === "k").onFire();
    expect(mutate).toHaveBeenCalledWith({ session_id: "sess-1" });
  });

  it("o opens full session detail", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const bindings = props.setShortcuts.mock.calls.at(-1)?.[0];
    expect(() => bindings.find((b: { key: string }) => b.key === "o").onFire()).not.toThrow();
  });

  it("c copies scrollback", () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const bindings = props.setShortcuts.mock.calls.at(-1)?.[0];
    bindings.find((b: { key: string }) => b.key === "c").onFire();
    expect(writeText).toHaveBeenCalledWith("hello\nworld");
  });

  it("Home scrolls to top and turns tail off", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const bindings = props.setShortcuts.mock.calls.at(-1)?.[0];
    bindings.find((b: { key: string }) => b.key === "Home").onFire();
    expect(props.setArgs).toHaveBeenCalledWith({ sessionId: "sess-1", tail: false });
  });

  it("End scrolls to bottom and turns tail on", () => {
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const bindings = props.setShortcuts.mock.calls.at(-1)?.[0];
    bindings.find((b: { key: string }) => b.key === "End").onFire();
    expect(props.setArgs).toHaveBeenCalledWith({ sessionId: "sess-1", tail: true });
  });

  it("unmount clears shortcuts", () => {
    const props = baseProps();
    const { unmount } = render(<SessionPeekPane {...props} />);
    unmount();
    expect(props.setShortcuts).toHaveBeenLastCalledWith([]);
  });
```

- [ ] **Step 2: Run tests, confirm the new ones fail**

Run: `cd dashboard && npm run test -- src/panes/session-peek/__tests__/index.test.tsx`
Expected: FAIL on the 8 new shortcut tests (component still calls `setShortcuts([])`).

- [ ] **Step 3: Wire real shortcuts**

In `dashboard/src/panes/session-peek/index.tsx`, replace the shortcuts effect:

```tsx
  useEffect(() => {
    setShortcuts([]);
    return () => setShortcuts([]);
  }, [setShortcuts]);
```

with:

```tsx
  useEffect(() => {
    setShortcuts([
      {
        key: "space",
        label: "Toggle follow tail",
        onFire: () => setArgs({ ...args, tail: !tail }),
      },
      { key: "k", label: "Kill session", onFire: doKill },
      { key: "o", label: "Open full session detail", onFire: openFullSession },
      { key: "c", label: "Copy scrollback", onFire: copyScrollback },
      {
        key: "Home",
        label: "Scroll to top",
        onFire: () => {
          setArgs({ ...args, tail: false });
          if (boxRef.current) boxRef.current.scrollTop = 0;
        },
      },
      {
        key: "End",
        label: "Scroll to bottom",
        onFire: () => {
          setArgs({ ...args, tail: true });
          if (boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
        },
      },
    ]);
    return () => setShortcuts([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tail, confirmingKill, exited]);
```

- [ ] **Step 4: Run tests and confirm they pass**

Run: `cd dashboard && npm run test -- src/panes/session-peek/__tests__/index.test.tsx`
Expected: PASS (26 tests: 4 component + 8 toolbar + 8 shortcuts + 6 manifest).

- [ ] **Step 5: Typecheck + lint**

Run: `cd dashboard && npm run typecheck && npm run lint -- src/panes/session-peek`
Expected: both exit 0.

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/panes/session-peek/index.tsx dashboard/src/panes/session-peek/__tests__/index.test.tsx
git commit -m "feat(dashboard): session-peek pane — keyboard shortcuts"
```

---

## Task 9: Session-exited banner + kill-disable-after-exit verification

**Files:**
- Modify: `dashboard/src/panes/session-peek/__tests__/index.test.tsx`

The exited banner and kill-disable behavior are already implemented (Task 7's
`exited` computation and JSX, driven by `useSession().lifecycle`, per pane spec §8). This
task is the dedicated test coverage for that behavior plus the frame-retention guarantee
("buffered peek frames stay visible (nothing cleared)", pane spec §8), which no earlier
task's tests exercise together.

**Interfaces:**
- Consumes: nothing new — exercises `SessionPeekPane` as already implemented.

- [ ] **Step 1: Add failing exited-state tests**

Add to the component `describe` block, above its closing `});`:

```tsx
  it("shows the exited banner and keeps frames visible when lifecycle is exited", () => {
    mockUseSession.mockReturnValue({ data: { lifecycle: "exited" } });
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText("Session exited — showing last scrollback.")).toBeInTheDocument();
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(screen.getByText("world")).toBeInTheDocument();
  });

  it("shows the exited banner for terminated lifecycle too", () => {
    mockUseSession.mockReturnValue({ data: { lifecycle: "terminated" } });
    render(<SessionPeekPane {...baseProps()} />);
    expect(screen.getByText("Session exited — showing last scrollback.")).toBeInTheDocument();
  });

  it("does not show the exited banner while running", () => {
    mockUseSession.mockReturnValue({ data: { lifecycle: "running" } });
    render(<SessionPeekPane {...baseProps()} />);
    expect(
      screen.queryByText("Session exited — showing last scrollback."),
    ).not.toBeInTheDocument();
  });

  it("does not show the exited banner on a transient stream error alone", () => {
    mockUseSession.mockReturnValue({ data: { lifecycle: "running" } });
    mockUseTranscriptStream.mockReturnValue({
      entries: [frame("hello", 0)],
      status: "error",
      error: "stream error (EventSource will retry)",
    });
    render(<SessionPeekPane {...baseProps()} />);
    expect(
      screen.queryByText("Session exited — showing last scrollback."),
    ).not.toBeInTheDocument();
  });

  it("toggle-tail and open-full stay enabled while exited", () => {
    mockUseSession.mockReturnValue({ data: { lifecycle: "exited" } });
    const props = baseProps();
    render(<SessionPeekPane {...props} />);
    const actions = props.setToolbar.mock.calls.at(-1)?.[0];
    expect(actions.find((a: { id: string }) => a.id === "toggle-tail").disabled).toBeFalsy();
    expect(actions.find((a: { id: string }) => a.id === "open-full").disabled).toBeFalsy();
  });
```

- [ ] **Step 2: Run tests, confirm they fail or pass and diagnose accordingly**

Run: `cd dashboard && npm run test -- src/panes/session-peek/__tests__/index.test.tsx`

These tests exercise behavior implemented in Task 7 — expected result is PASS (31 tests:
9 component + 8 toolbar + 8 shortcuts + 6 manifest). If any fail, the bug is in Task 7's
`exited` computation or JSX (`dashboard/src/panes/session-peek/index.tsx`) — fix it there,
not by weakening the test.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/panes/session-peek/__tests__/index.test.tsx
git commit -m "test(dashboard): session-peek pane — exited-state coverage"
```

---

## Task 10: Remaining component coverage + full parity verification

**Files:**
- Modify: `dashboard/src/panes/session-peek/__tests__/index.test.tsx`

Fills the gaps between what Tasks 6–9 exercised and the full list in pane spec §10:
sticky-scroll-away-from-bottom, unmount cleanup already covered per-registration (Tasks
7–8) but not asserted together, and a final run of every test file this plan touched.

**Interfaces:** none new.

- [ ] **Step 1: Add the sticky-scroll test**

Add to the component `describe` block, above its closing `});`. This exercises `onScroll`
directly (jsdom does not compute real layout, so the test sets `scrollHeight` /
`clientHeight` / `scrollTop` via `Object.defineProperty` the way RTL-based scroll tests
conventionally do, then fires a native `scroll` event):

```tsx
  it("scrolling away from bottom while tail is true calls setArgs with tail:false", () => {
    const props = baseProps();
    const { container } = render(<SessionPeekPane {...props} />);
    const scrollBox = container.querySelector(".overflow-y-auto") as HTMLDivElement;
    Object.defineProperty(scrollBox, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(scrollBox, "clientHeight", { value: 300, configurable: true });
    Object.defineProperty(scrollBox, "scrollTop", { value: 200, configurable: true });
    scrollBox.dispatchEvent(new Event("scroll"));
    expect(props.setArgs).toHaveBeenCalledWith({ sessionId: "sess-1", tail: false });
  });

  it("does not call setArgs when already near the bottom", () => {
    const props = baseProps();
    const { container } = render(<SessionPeekPane {...props} />);
    const scrollBox = container.querySelector(".overflow-y-auto") as HTMLDivElement;
    Object.defineProperty(scrollBox, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(scrollBox, "clientHeight", { value: 300, configurable: true });
    Object.defineProperty(scrollBox, "scrollTop", { value: 690, configurable: true }); // slack 10 < 24
    scrollBox.dispatchEvent(new Event("scroll"));
    expect(props.setArgs).not.toHaveBeenCalled();
  });
```

- [ ] **Step 2: Run this file, confirm the two new tests pass (33 total) and nothing
      else regressed**

Run: `cd dashboard && npm run test -- src/panes/session-peek/__tests__/index.test.tsx`
Expected: PASS, 33 tests (11 component + 8 toolbar + 8 shortcuts + 6 manifest).

- [ ] **Step 3: Run the full frontend test suite**

Run: `cd dashboard && npm run test`
Expected: PASS — includes `src/panes/__tests__/registry.test.ts` (4 tests) and
`src/panes/session-peek/__tests__/index.test.tsx` (33 tests), 37 tests total, 0 failures.

- [ ] **Step 4: Run the backend parity test**

Run: `pytest tests/test_pane_registry_parity.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Full typecheck, lint, build**

Run: `cd dashboard && npm run typecheck && npm run lint && npm run build`
Expected: all three exit 0. A `zod`-shaped type error here most likely means
`sessionPeekArgsSchema`'s inferred type drifted from `PaneViewProps<SessionPeekArgs>`'s
expectations — re-check `manifest.ts` against Task 3 verbatim.

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/panes/session-peek/__tests__/index.test.tsx
git commit -m "test(dashboard): session-peek pane — sticky-scroll coverage + full suite green"
```

---

## Task 11: Manual verification checklist

No code changes — this task is a documented, human-run pass confirming the shipped pane
behaves correctly against a live daemon, since the dashboard has no E2E infra (interface
spec §9.2 note: "manual verification for cross-page flows"). Because `<ShellPane>` (the
host component) doesn't exist yet in this repo, steps 1–2 below drive `SessionPeekPane`
through a temporary throwaway harness route rather than the real shell — delete that
route before merging.

- [ ] **Step 1: Add a temporary manual-test route**

Create `dashboard/src/dev/SessionPeekHarness.tsx` (temporary, not part of the shipped
bundle — delete in Step 4):

```tsx
import { useState } from "react";
import SessionPeekPane from "../panes/session-peek";
import type { SessionPeekArgs } from "../panes/session-peek/manifest";

export default function SessionPeekHarness() {
  const [args, setArgs] = useState<SessionPeekArgs>({ sessionId: "REPLACE-WITH-REAL-SESSION-ID" });
  return (
    <div style={{ height: "80vh", width: 480, border: "1px solid #333" }}>
      <SessionPeekPane
        args={args}
        close={() => console.log("close() called")}
        setArgs={setArgs}
        setToolbar={(actions) => {
          (window as unknown as { __toolbar: unknown }).__toolbar = actions;
        }}
        setShortcuts={(bindings) => {
          (window as unknown as { __shortcuts: unknown }).__shortcuts = bindings;
        }}
      />
    </div>
  );
}
```

Temporarily add a route for it in `dashboard/src/App.tsx` (find the existing route list
and add one entry, e.g. `<Route path="/_dev/session-peek" element={<SessionPeekHarness
/>} />` inside the same `<Routes>` tree as `/sessions/:id`) — this edit is reverted in
Step 4, not committed.

- [ ] **Step 2: Run against a live daemon with a running session**

Start the daemon (`./run.sh start` from repo root) and confirm at least one session is
running (`aq agent list` or check `/work/agents`). Edit the harness's
`REPLACE-WITH-REAL-SESSION-ID` to that session's id. Run:

```bash
cd dashboard && AQ_API_TARGET=http://127.0.0.1:8091 npm run dev
```

(adjust the port to match the running daemon). Visit `/_dev/session-peek`. Confirm:

- Peek frames stream in and the console auto-scrolls to bottom as new frames arrive.
- Scrolling up manually stops the auto-scroll (open devtools console, run
  `window.__toolbar.find(a => a.id === "toggle-tail").label` — should now read
  `"Follow tail"`, confirming `args.tail` flipped to `false` from the scroll).
- Clicking `window.__toolbar.find(a => a.id === "toggle-tail").onClick()` in the devtools
  console re-enables tail and the label flips back to `"Pause tail"`.
- `window.__toolbar.find(a => a.id === "copy-scrollback").onClick()` — check the OS
  clipboard contains the joined peek text.
- `window.__toolbar.find(a => a.id === "kill-session").onClick()` once — label becomes
  `"Confirm kill?"`; call it again — confirm the session actually terminates (check `aq
  agent list` or the session's lifecycle via `aq session show <id>` if that command
  exists, or the daemon logs).
- After the kill, `useSession`'s next 15s poll should flip `lifecycle` to `exited` /
  `terminated` — confirm the amber "Session exited — showing last scrollback." banner
  appears and the kill toolbar entry is disabled (its `onClick` should log to console but
  the button itself would be visually disabled in a real toolbar host — the harness above
  doesn't render buttons, so confirm via `window.__toolbar.find(a => a.id ===
  "kill-session").disabled === true` in devtools).
- Peek frames buffered before the kill remain visible in the console (nothing clears on
  exit).

- [ ] **Step 3: Confirm keyboard-handler wiring is self-consistent**

Since there's no real shell to dispatch `key` events, verify programmatically instead:
in the devtools console, run `window.__shortcuts.map(b => b.key)` and confirm it prints
`["space", "k", "o", "c", "Home", "End"]` — matches pane spec §6's shortcut table exactly.
Call `window.__shortcuts.find(b => b.key === "space").onFire()` and re-check the
toolbar's `toggle-tail` label flipped, same as the manual click above.

- [ ] **Step 4: Remove the temporary harness**

```bash
git diff dashboard/src/App.tsx   # confirm only the one route line changed
git checkout -- dashboard/src/App.tsx
rm dashboard/src/dev/SessionPeekHarness.tsx
rmdir dashboard/src/dev 2>/dev/null || true
```

Confirm nothing from this task remains staged or tracked:

Run: `git status --porcelain dashboard/src/App.tsx dashboard/src/dev`
Expected: no output (both are back to their pre-Task-11 state; `dashboard/src/dev` no
longer exists).

No commit for this task — it's verification-only and its own scaffolding is explicitly
reverted in Step 4.

---

## Self-Review

**1. Spec coverage** (`2026-08-22-pane-session-peek-design.md`):
- §3 manifest — Task 3.
- §4 args + validation — Task 3 (schema), Task 6/7 (`args.tail ?? true` effective
  default computed in the component, per pane spec §3's `.optional()` note).
- §5.1 file layout — Tasks 3, 6 (no `hooks.ts`, matches spec).
- §5.2/§5.3 composition + follow-tail model — Task 6.
- §5.4 monospace rendering / DOM shape — Task 5 (extraction) + Task 6 (reuse).
- §6 toolbar + shortcuts tables — Tasks 7, 8 verbatim id/key lists.
- §7 data + queries — Task 6 (`useTranscriptStream`, `useSession`, `useSessionKill`,
  unsubscribe-by-unmount, no new hooks/endpoints).
- §8 loading/error/exited states — Task 6 (loading/error), Task 9 (exited banner +
  frame-retention + kill-disable, plus the "not off SSE status" distinction tested
  explicitly).
- §9 agent-push examples — documented in Global Constraints as accurately describing
  future behavior once the separately-scoped `--pane-open` plumbing lands; no code task
  needed in this plan since `open("session-peek", {...})` (the call the future dispatcher
  makes) already works after Task 6.
- §10 tests — Tasks 3, 6, 7, 8, 9, 10 collectively cover every bullet in the spec's test
  list, including the manifest tests, the `space`/`k`/`o`/`c`/`Home`/`End` shortcuts, the
  arm/confirm kill sequence, clipboard, exited state, error state, loading state, and
  unmount cleanup for both toolbar and shortcuts.
- §11 implementation checklist — every checkbox maps to a task except the `--pane-open`
  CLI wiring, explicitly deferred (Global Constraints) and the click-through call sites,
  explicitly out of scope per the spec's own checklist note.
- §12 open questions — no action needed; all three are explicitly "not addressed here" /
  "unexercised, not unsupported" in the spec itself, so no task was owed to them.

**2. Placeholder scan:** none — every step has runnable code, an exact command, or (Task
11) a concrete devtools expression with an expected value. The one guarded exception,
Task 6 Step 3's `data-testid="_unused"` line, is not a "TBD" — it's a named, temporary,
self-documenting keep-alive that Task 7 Step 3 deletes by replacing the whole file, and
its purpose and removal point are stated inline.

**3. Type consistency:** `SessionPeekArgs`, `sessionPeekArgsSchema`, and `manifest` are
defined once in Task 3 and imported unchanged by every later task. `PaneViewProps`,
`PaneToolbarAction`, `ShortcutBinding`, `PaneManifest`, `PaneEntry` are defined once in
Task 2 and used with identical field names throughout (`setArgs`, `setToolbar`,
`setShortcuts`, `args`, `close`). The toolbar action ids (`toggle-tail`,
`copy-scrollback`, `open-full`, `kill-session`) and shortcut keys (`space`, `k`, `o`, `c`,
`Home`, `End`) introduced in Task 7/8 match the tables in pane spec §6 verbatim and are
referenced identically by their own tests and by Task 9/10's additional tests.

**Deviations from the per-pane spec, called out explicitly:**
- `open_shortcut: null` (spec's manifest snippet) → omitted field (this plan) — reconciled
  in Task 3 in favor of the interface spec's normative rule; no behavioral difference.
- Spec's single-pass `index.tsx` code sample (pane spec §5.2) is built here across Tasks
  6–8 via TDD instead of written whole-cloth in one step — same end state, different path,
  chosen to satisfy the writing-plans skill's red-green-commit cycle per task.
