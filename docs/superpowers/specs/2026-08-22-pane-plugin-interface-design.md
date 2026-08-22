# Pane Plugin Interface — Design

**Status:** design (approved by brainstorming pass 2026-08-22).
**Depends on:** `2026-08-22-dashboard-shell-v2-design.md` (shell primitives).
**Consumed by:** every `2026-08-22-pane-<view>-design.md` (nine pane
views, each implements this contract).

## 1. Goal

Define the contract every pane view implements so the shell can host
them uniformly, sub-agents can build them in parallel without
coordinating, and a future upgrade path to true runtime plugins doesn't
require rewriting any view.

This spec is the **only** thing every per-pane spec depends on. Every
per-pane spec is a self-contained implementation of this contract for
one view.

## 2. Non-goals

- Not runtime module federation. Views ship in the dashboard bundle.
- Not a permission / capability system. The daemon's HTTP scope check
  is the enforcement point; a pane view is client code and inherits the
  user's session scope.
- Not a settings / preferences UI for the pane. Per-view state persists
  in `localStorage`; there's no user-facing "manage pane views" page.
- Not a marketplace or discovery mechanism for third-party views.

## 3. What is a pane view

A pane view is a self-contained React component that renders in the
shell's `<ShellPane>` when the shell's pane state points at that view's
id. Every view lives at:

```
dashboard/src/panes/<view-id>/
├── manifest.ts         # id, name, args schema, keyboard, icon
├── index.tsx           # default export = React component
├── args.ts             # (optional) zod schema module if it's large
├── hooks.ts            # (optional) view-local hooks
└── __tests__/
    └── index.test.tsx  # component + manifest tests
```

The `<view-id>` is the same string that appears as `PaneState.view` in
the shell store and as `open_pane.view` in the agent-push message frame.

## 4. Manifest

```ts
// dashboard/src/panes/<view-id>/manifest.ts

import { z } from "zod";
import type { ComponentType } from "react";
import type { LucideIcon } from "lucide-react";  // or a heroicons wrapper

export interface PaneManifest<TArgs = unknown> {
  /** Stable id — matches the directory name; used everywhere. */
  id: string;

  /** Human name shown in the pane header + palette. */
  name: string;

  /** Short description used in palette + cheat sheet. */
  description: string;

  /** Icon shown in header + palette. */
  icon: LucideIcon;  // (or the heroicons type used across the dashboard)

  /**
   * zod schema for the args object. Runtime-validated on every open
   * call — invalid args fail loudly instead of rendering a broken
   * pane. `undefined` schema means "no args required".
   */
  args_schema?: z.ZodType<TArgs>;

  /**
   * Optional keyboard shortcut that OPENS this view.
   * Registered globally via `useShortcuts`.
   * Example: "$mod-shift-D" for the diff view.
   */
  open_shortcut?: string;

  /**
   * How the view relates to routes.
   * - "cross-route" (default): pane content persists across route
   *   navigation.
   * - "route-scoped": pane closes automatically on route change.
   * Most views are cross-route so the user can chat about pane
   * content while navigating.
   */
  route_scope?: "cross-route" | "route-scoped";

  /**
   * Whether the agent may push this view (via the pane_open message
   * frame). Some views (e.g. contextual-settings) may want to be
   * user-only. Default true.
   */
  agent_pushable?: boolean;

  /**
   * Palette action label. If null, the view is not registered as a
   * palette action (e.g. task-detail is only opened via click or
   * agent push, not searchable in the palette).
   */
  palette_label?: string | null;

  /**
   * Palette section this view's action belongs to. Ignored when
   * palette_label is null.
   */
  palette_section?: string;
}
```

Every view exports `manifest` as a named export from `manifest.ts`.

### 4.1 Registry

At build time, `dashboard/src/panes/registry.ts` imports every
`dashboard/src/panes/*/manifest.ts` and every `.../index.tsx`, and
assembles:

```ts
export interface PaneEntry {
  manifest: PaneManifest;
  Component: ComponentType<PaneViewProps>;
}

export const PANE_REGISTRY: Record<string, PaneEntry> = {
  "task-detail":         { manifest: taskDetailManifest,   Component: TaskDetailPane },
  "diff-review-changes": { manifest: diffManifest,          Component: DiffPane },
  // ... nine entries total when all views are shipped
};
```

Uses Vite's `import.meta.glob("./*/manifest.ts", { eager: true })`
pattern. Zero runtime discovery; the registry is a static object the
shell reads.

Adding a new pane view: create the directory, drop in `manifest.ts` +
`index.tsx`, and the registry picks it up on next build.

### 4.2 Registry validation

At registry build, each entry is validated:
- `manifest.id` matches the directory name.
- `manifest.id` is unique across the registry.
- Component default export exists.
- If `open_shortcut` is set, it doesn't collide with another view's
  `open_shortcut` or with any reserved shell shortcut.

Violations throw at module-eval time; the build fails.

## 5. Component contract

Every pane view exports a default component with this signature:

```ts
// dashboard/src/panes/<view-id>/index.tsx

interface PaneViewProps<TArgs = unknown> {
  /** The args object passed at open time, already zod-validated. */
  args: TArgs;

  /**
   * Close the pane. Wraps the shell's `useShellPaneStore().close()`
   * — views should call this from a close button, on an intentional
   * "done" action, or when the args become invalid mid-render.
   */
  close: () => void;

  /**
   * Update the args for THIS OPEN pane without closing + re-opening.
   * Used when a view's internal navigation (e.g. clicking a file in
   * the file-browser view) should change its own args. Zod-validated
   * against the same schema.
   */
  setArgs: (next: TArgs) => void;

  /**
   * Register toolbar action buttons to appear in the pane header.
   * Called during render; the shell renders whatever the view last
   * registered. Passing `[]` clears the toolbar.
   */
  setToolbar: (actions: PaneToolbarAction[]) => void;

  /**
   * Register per-entity shortcuts scoped to this pane. Wraps
   * `useEntityShortcuts` under the hood but scopes to pane focus.
   */
  setShortcuts: (bindings: ShortcutBinding[]) => void;
}

interface PaneToolbarAction {
  id: string;
  label: string;
  icon?: LucideIcon;
  onClick: () => void;
  disabled?: boolean;
}

interface ShortcutBinding {
  key: string;      // normalized form, e.g. "$mod-r"
  label: string;    // shown in cheat sheet
  onFire: () => void;
}
```

### 5.1 Toolbar

The shell's pane header always renders `[icon] name` on the left and
`×` (close) on the right. Views can push additional actions between
them via `setToolbar`. Examples:
- Diff view pushes `[Copy diff] [Open PR]`.
- File browser pushes `[Refresh] [Copy path]`.
- Playbook run inspector pushes `[Resume] [Cancel]`.

Toolbar actions are keyboard-accessible via `Tab`; each action button
carries a `data-hotkey` attribute if declared, which the shortcut
system reads for cheat-sheet enumeration.

### 5.2 Shortcuts

Views declare pane-scoped shortcuts via `setShortcuts`. Bindings only
fire when the pane holds focus. The cheat sheet (`?`) shows them under
the current view's section when the pane is focused.

### 5.3 setArgs

`setArgs` handles the case where a view's own UI changes its args
without re-invoking the shell's `open` call. Example: file browser
starts at `{ workspace_id, path: "src/" }`, user clicks into
`src/api/`, and the view updates its own args to
`{ workspace_id, path: "src/api/" }`. This preserves the pane's
open state (no flicker, no reopen) and keeps the args + URL in sync
if any view chooses to reflect state in the URL.

`setArgs` re-validates against `manifest.args_schema`; validation
failure throws (view is buggy; not a runtime user error).

## 6. Opening the pane

### 6.1 Programmatic (shell + user surfaces)

```ts
const { open, close, state } = useShellPane();

open("task-detail", { taskId: "abc-123" });
// or
open("diff-review-changes", { taskId: "abc-123", from: "main", to: "HEAD" });
```

`open(viewId, args, opts?)`:
- Look up `PANE_REGISTRY[viewId]`. Missing view → console.error + no-op.
- Validate `args` against `manifest.args_schema` (if present). Zod
  failure → console.error + no-op.
- Set pane state to `{ kind: "open", view: viewId, args, width }`.
- Closes the drawer if open (see shell spec §3.3).
- Trigger optional `on_open` telemetry (future hook).

`opts` is reserved for future extensions (initial focus, temporary
width override, etc.); initial ship uses no options.

### 6.2 From click-through in a list

Task tables, agent tables, session lists, gate lists, all call `open`
directly. Convention: `Enter` on a focused row also calls `open`.

### 6.3 From a palette action

Palette registers per-view actions using the view's `palette_label`.
When invoked, the action either:
- Opens with defaulted args (e.g. diff view for the current task if
  one is focused).
- Prompts for args inline in the palette (future — v2 refinement).

Palette section defaults to "Panes" if `palette_section` is unset.

### 6.4 From a keyboard shortcut

If manifest sets `open_shortcut`, the shell registers it globally.
Handler resolves the args from current focus context (implementation:
each view exposes a `resolveDefaultArgs(context)` optional helper in
its manifest — TBD if needed; initial views don't need it).

### 6.5 From an agent push (the message frame)

Supervisor emits a message with a special body_kind. Concrete shape
of the frame:

```json
{
  "message_id": "...",
  "from_kind": "session",
  "from_id": "supervisor-global",
  "to_kind": "user",
  "to_id": "dashboard",
  "thread_id": "dashboard:global",
  "body_kind": "pane_open",
  "body": "opened the diff on the right →",
  "pane_open": {
    "view": "diff-review-changes",
    "args": { "task_id": "abc-123" }
  },
  "created_at": 1234567890
}
```

`body_kind: "pane_open"` is a new column on the `messages` table
(nullable text, empty for normal chat frames — no migration needed
beyond a column add). The WS `message.sent` payload gains an optional
`pane_open: { view, args }` field.

Client behavior when a `message.sent` event arrives with `pane_open`:
- Chat surface renders an inline chip in the transcript (`InlineEventCard`
  gets a new `pane_open` case) with the view id + "opened →" affordance.
  Clicking the chip re-opens the pane.
- `useChatTranscript` also dispatches `pane.open(view, args)` at
  arrival time, subject to `manifest.agent_pushable !== false`.
- If the message's `to_kind !== "user"` (e.g. the frame is from a
  worker session, not the supervisor talking to the user), the client
  does NOT auto-open. The chip still renders in the transcript.

Server-side helpers to emit the frame from the supervisor:

```
aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "opened the diff on the right →" \
    --pane-open '{"view": "diff-review-changes", "args": {"task_id": "abc-123"}}'
```

The `--pane-open` flag is added to `aq message send` (CLI + command).
The `_cmd_message_send` handler validates the JSON, refuses if
`view` doesn't exist in a shipped views registry (server keeps a
mirror-list; see §7), and refuses if `agent_pushable` is false.

## 7. Server-side view mirror

The daemon needs to know the view registry to validate `--pane-open`
frames. Two options:

- **A. Static list.** A `src/panes/registry.py` module ships a static
  set of `{"view_id": {"agent_pushable": bool}}` for every view. Kept
  in sync manually with the frontend registry (one line per view).
- **B. Generated from the frontend registry.** A build step reads
  every `dashboard/src/panes/*/manifest.ts` and emits a Python
  companion; CI enforces the two are in sync.

Initial ship: **A**, because (a) 9 views is a small hand-maintained
set, (b) adding a view is a two-line change (manifest + Python entry),
(c) the sync check can be added as a test that reads both lists and
asserts equality. Upgrade to B if the view count grows to where hand-
sync gets fragile.

The test:

```python
# tests/test_pane_registry_parity.py
def test_frontend_and_backend_registries_match():
    frontend_ids = _read_frontend_manifest_ids()
    backend_ids = set(SERVER_PANE_REGISTRY.keys())
    assert frontend_ids == backend_ids
```

## 8. Lifecycle summary

**Open:**
- shell calls `Component` with `{args, close, setArgs, setToolbar, setShortcuts}`.
- Component renders. May call `setToolbar` / `setShortcuts` during
  render (idempotent).
- Component may subscribe to WS events, React Query hooks, or its own
  local state.

**Args change (via setArgs):**
- Component re-renders with new `args`. Component owns whether it
  refetches, resets scroll, keeps history, etc.

**Route change:**
- If `manifest.route_scope === "route-scoped"`, shell calls `close()`.
- Otherwise, pane stays open across the route change. Component
  re-mounts only if React's reconciliation decides to (typically it
  doesn't — the pane is outside the route outlet).

**Close:**
- Component unmounts. Cleanup happens in the component's normal effect
  cleanups.
- `setToolbar([])` and `setShortcuts([])` unregister automatically on
  unmount.

**Width change:**
- Shell handles this; components don't need to react to it (their
  container just gets narrower/wider).

## 9. Testing shape

### 9.1 Per-view

Every pane view ships with tests under `__tests__/`:

- Manifest tests:
  - `manifest.id` matches directory name.
  - `manifest.args_schema` (if present) accepts valid args, rejects
    invalid.
  - `manifest.open_shortcut` (if present) is a valid normalized form.
- Component tests:
  - Renders with valid args without crashing.
  - Calls `close` when its close-affordance fires.
  - If it registers toolbar actions or shortcuts, they're invocable.
  - Contract-relevant behaviors (view-specific; each per-pane spec
    lists these).

### 9.2 Registry

A shared test file `dashboard/src/panes/__tests__/registry.test.ts`:

- All views declared in a manifest list are actually resolvable.
- `manifest.id` is unique.
- No `open_shortcut` collisions.
- Parity with server-side pane list (imports the Python-side list via
  a shared JSON snapshot).

### 9.3 Shell-pane integration

Lives in the shell spec's test suite, not per-view.

## 10. Building a new pane view — checklist

For a sub-agent implementing one of the 9 per-view specs, the checklist
is:

- [ ] Create `dashboard/src/panes/<view-id>/` directory.
- [ ] Write `manifest.ts` with `id`, `name`, `description`, `icon`,
      `args_schema`, and any optional fields the per-view spec calls
      for.
- [ ] Write `index.tsx` implementing the component contract (§5).
      Use `PaneViewProps<TArgs>` typing to catch mismatches.
- [ ] Add the view id to `src/panes/registry.py` (server-side mirror,
      §7) with `agent_pushable` set per the per-view spec.
- [ ] Write component tests under `__tests__/`.
- [ ] Run the parity test to confirm the two registries match.
- [ ] Follow any view-specific requirements in the per-view spec.

Everything else (shell integration, routing, keyboard registration,
palette wiring, agent-push handling) is provided by the shell — a view
never needs to touch shell code.

## 11. Open questions

- **Args prompting in palette** — some views (diff-review, file-
  browser) need args the user doesn't type as a raw JSON blob. The
  palette should have a follow-up prompt UX ("which task?", "which
  file?") for these. Deferred to v2 refinement — v1 palette actions
  either open with defaults derived from current focus or don't
  register a palette action at all.
- **View-supplied loading/error skeletons.** Currently each view
  provides its own loading + error states inline. If we want a shared
  skeleton primitive, that lives in the shell spec, not here.
- **Nested panes.** A view might want to open another view (e.g. a
  proposal preview lets you jump to a task-detail of a proposed task).
  For v1 this replaces the current pane content — same primitive,
  same slot, different view id. Back-navigation not modeled. Deferred.
