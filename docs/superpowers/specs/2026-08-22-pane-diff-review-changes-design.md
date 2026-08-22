# Pane View — `diff-review-changes` — Design

**Status:** design (approved by brainstorming pass 2026-08-22).
**Depends on:**
- `2026-08-22-dashboard-shell-v2-design.md` (shell primitives, `<ShellPane>`).
- `2026-08-22-pane-plugin-interface-design.md` (manifest schema, component
  contract, registry mechanics — **this spec implements that contract for
  one view and does not redefine it**).

## 1. Goal

Ship the `diff-review-changes` pane view: a v1 ship-priority view that lets
a user (or the supervisor, via agent-push) review a task's changed files —
file list + diff/preview — inside the shell's right-side pane, without
leaving whatever they're doing in the center.

This is a re-framing, not a rewrite. The file-list-left / preview-right
experience already exists as `dashboard/src/components/TaskFilesPanel.tsx`,
consumed today by the standalone route `dashboard/src/pages/TaskFiles.tsx`
(`/tasks/:id/files`). That route and component are Phase 5 work and stay in
place unchanged — this pane view becomes a second, narrower-width consumer
of the same data-fetch functions (`fetchTaskFiles`, `fetchTaskFileText`) and
the same `MarkdownPreview` renderer, with its own layout tuned for the
pane's ~480px default width instead of the full page.

## 2. Non-goals

- Not touching `TaskFilesPanel.tsx` or `TaskFiles.tsx` — both keep serving
  the full-page route as-is. This pane view is a sibling consumer of the
  same `dashboard/src/api/taskFiles.ts` functions, not a replacement.
- Not adding syntax highlighting. `TaskFilesPanel` renders plain `<pre>`
  text today; this view matches that fidelity. A follow-up could add
  `shiki`/`prism` to both consumers at once — out of scope here.
- Not adding a real unified-diff view (with +/− line markers inline). The
  existing `/file` endpoint returns whole-file content, not a diff hunk
  stream; per-line diff rendering would need a new backend endpoint. This
  view shows the changed-file list (with existing +/− counts from `/files`)
  and a whole-file preview on selection — same fidelity as `TaskFilesPanel`
  today.
- Not implementing the shell primitives (`<ShellPane>`, `useShellPane`,
  `useShortcuts`, `useEntityShortcuts`) — those come from Phase B and are
  assumed to exist per the shell + plugin-interface specs.
- Not building the palette's args-prompting UX (see plugin-interface spec
  §11, "Args prompting in palette" — deferred). The palette action for this
  view opens with defaulted args derived from focus context, same as any
  other v1 view.

## 3. Manifest

```ts
// dashboard/src/panes/diff-review-changes/manifest.ts
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

Notes on deviations from the plugin-interface spec's illustrative snippet:

- The plugin-interface spec's `PaneManifest` example types `icon` as
  `LucideIcon`. Per `dashboard/CLAUDE.md` ("Icons: `@heroicons/react/24/outline`
  ... Don't introduce other icon libraries"), this view uses
  `HeroIcon` (the heroicons component type used elsewhere in the dashboard —
  see `PaneManifest`'s own comment allowing "a heroicons wrapper"). The
  shared `PaneManifest<TArgs>` type in `dashboard/src/panes/types.ts` should
  type `icon` as the heroicons component type, not `LucideIcon`; every pane
  view (not just this one) needs this, so it's called out here as a note
  for whichever view lands the shared `types.ts` file first, not a
  per-view deviation.
- `route_scope: "cross-route"` is explicit even though it matches the
  manifest default — reviewing a diff while navigating (e.g. checking the
  task's parent task, or a related task in Command Center) is exactly the
  persist-across-routes use case the shell spec calls out in §5.3.

## 4. Args + validation

Args object: `{ taskId: string, base?: string, filePath?: string }`.

- `taskId` — required. The task whose worktree diff to show. No format
  validation beyond non-empty (task ids are opaque strings elsewhere in the
  dashboard — see `TaskFiles.tsx`'s `useParams<{ taskId }>()`, which does
  no validation either).
- `base` — optional. When present, overrides the diff base used for
  fetching content comparisons. **v1 scope note:** `GET /api/tasks/{id}/files`
  does not currently accept a `base` query param — it returns whatever base
  the daemon computed server-side (`TaskFilesResponse.base`). This view
  accepts `base` in its args (per the args-schema requirement in this
  view's assignment) and displays it in the toolbar/header as an
  informational override label ("base: `<base>`" vs the daemon's own
  `data.base`), but does **not** yet re-fetch with a different base — see
  Open Questions §12. This keeps the arg's shape stable for a later backend
  change without blocking v1 ship.
- `filePath` — optional. When present, pre-selects that file in the list on
  open (equivalent to the user clicking that row). Must match an entry in
  the fetched `files[].path` list to take effect; if it doesn't match
  (stale arg, renamed file), the view falls back to no selection and does
  not error.

Zod schema mirrors the shell's runtime-validation contract (plugin-interface
spec §4, `args_schema`): `open("diff-review-changes", {})` (missing
`taskId`) fails validation and the shell no-ops with a console error before
this component ever mounts — the component itself does not need to
re-guard against a missing `taskId`.

## 5. Component

```
dashboard/src/panes/diff-review-changes/
├── manifest.ts
├── index.tsx
└── __tests__/
    └── index.test.tsx
```

No `args.ts` (schema is small, inlined in manifest.ts) and no `hooks.ts`
(the two queries below are simple enough to inline in `index.tsx`, matching
`TaskFilesPanel.tsx`'s current shape).

### 5.1 Layout

Two-column layout inside the pane body, sized for the pane's default
480px width (200px min / 800px max, per shell spec §5.4):

```
┌ file list (~40%) ─┬─ preview (~60%) ────────────┐
│ [/ filter......]  │  src/api/taskFiles.ts        │
│ M taskFiles.ts     │  ┌──────────────────────┐   │
│ A newfile.py       │  │  <pre> or             │   │
│ D removed.md        │  │  <MarkdownPreview>    │   │
│                    │  └──────────────────────┘   │
└────────────────────┴──────────────────────────────┘
```

- Container: `flex h-full` (fills the pane body height, which the shell
  frame already constrains — see shell spec §5.5 header/body split).
- Left column: `w-2/5 min-w-[140px]` — file list, matching
  `TaskFilesPanel`'s existing row markup (status letter, path, +/− counts)
  but in a narrower column, so `truncate` on the path is load-bearing (the
  full-page version has more headroom; the pane version relies on it more).
- Right column: `flex-1` — preview area, matching `TaskFilesPanel`'s
  `MarkdownPreview` / `<pre>` split.
- **Narrow-pane collapse (<400px):** container switches to `flex-col`.
  File list becomes a fixed-height (`max-h-[40%]`) scrollable strip above
  the preview, which takes the remaining vertical space. Implementation:
  a `useContainerWidth`-style `ResizeObserver` on the pane body ref (new,
  small — this view doesn't get a width prop from the shell per the
  plugin-interface component contract, so it must measure its own
  container) toggling a `narrow` boolean at the 400px breakpoint. If the
  shell's `<ShellPane>` implementation (Phase B) later threads its current
  width down as a prop, this view swaps to that on the next pass — noted
  as the one piece of implicit coupling to a not-yet-built shell internal.
- Both columns reuse existing Tailwind classes from `TaskFilesPanel.tsx`
  (`rounded border border-gray-800 bg-gray-950`, `text-xs font-mono`, status
  color function) for visual consistency with the full-page view — same
  diff, different chrome.

### 5.2 File list row

Reused verbatim from `TaskFilesPanel.tsx`'s row markup (status color
function `statusColor`, `+N`/`-N` counts, `font-mono` path). Exported as a
small shared helper `statusColor(status: string): string` — this view
imports it directly from `TaskFilesPanel.tsx` (already a named-adjacent
export point; if `TaskFilesPanel.tsx` doesn't currently export it, promote
it to a named export as a one-line change, not a duplication) rather than
reimplementing the switch statement.

New in the pane version, not present in `TaskFilesPanel.tsx` today:

- A filter input (`/` shortcut focuses it — see §6). Filters the file list
  by substring match on `path`, case-insensitive. Purely client-side over
  the already-fetched `files[]` array — no new query.
- Keyboard row selection (`↑↓`/`Enter`) — `TaskFilesPanel.tsx` is
  click-only today; this view adds keyboard nav per §6 since pane content
  is expected to support the shell's list-motion vocabulary (shell spec
  §8.4).

### 5.3 Preview area

Identical branching to `TaskFilesPanel.tsx`'s preview column:

- No file selected → placeholder text ("Select a file to preview.").
- Loading → "Loading `<path>`…".
- Error → red error text with `(error as Error).message`.
- `.md` file (case-insensitive suffix, same `isMd` check as
  `TaskFilesPanel.tsx`) with `status === 200` → `<MarkdownPreview
  source={...} />`.
- Otherwise → `<pre>` with `whitespace-pre-wrap font-mono text-xs`.

The binary and error-status bodies (413/403/404, and the `reason: "binary"`
JSON case) are already normalized into plain display strings by
`fetchTaskFileText` in `dashboard/src/api/taskFiles.ts` (see §7) — e.g.
`"(binary file omitted (12 KB))"`, `"(forbidden path)"`. This view renders
whatever string comes back through the same `<pre>` branch; no new
binary-detection logic needed here, it already lives in the shared fetch
function both consumers call.

### 5.4 Full-page escape hatch

Toolbar exposes `[Open full-page view]` (see §6) which navigates to
`/tasks/:id/files` (the existing `TaskFiles.tsx` route) via
`useNavigate()`, carrying the current `selected` file forward as a
best-effort — `TaskFiles.tsx`/`TaskFilesPanel` don't currently accept a
pre-selected file via URL param, so this is a plain navigation to the task
id's files route (no selection carried over) unless `TaskFilesPanel` is
extended to accept an initial-selection prop as a small follow-up. Noted
in Open Questions §12; not blocking for v1.

## 6. Toolbar + shortcuts

### 6.1 Toolbar (via `setToolbar`)

Registered on every render (idempotent per plugin-interface spec §5.1):

```ts
setToolbar([
  { id: "refresh", label: "Refresh", icon: ArrowPathIcon, onClick: () => filesQ.refetch() },
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

`Refresh` re-runs both the file-list query and, if a file is selected, the
selected file's content query (`filesQ.refetch(); if (selected) fileQ.refetch();`).

### 6.2 Pane-scoped shortcuts (via `setShortcuts`)

```ts
setShortcuts([
  { key: "ArrowUp",   label: "Previous file", onFire: () => moveSelection(-1) },
  { key: "ArrowDown", label: "Next file",      onFire: () => moveSelection(1) },
  { key: "Enter",     label: "Open file",      onFire: () => openFocusedFile() },
  { key: "/",         label: "Filter files",   onFire: () => filterInputRef.current?.focus() },
  { key: "r",         label: "Refresh",        onFire: () => filesQ.refetch() },
]);
```

Bindings only fire while the pane holds focus, per plugin-interface spec
§5.2. `↑↓` move a `focusedIndex` cursor over the (filtered) file list
independent of `selected` (the previewed file); `Enter` promotes
`focusedIndex` to `selected` — this matches the shell's general list-motion
convention (shell spec §8.4: move focus, then `Enter` opens) rather than
selecting-on-arrow, so a user can arrow through the list without triggering
a fetch per keystroke.

## 7. Data + queries

Both endpoints are existing, unmodified:

- `GET /api/tasks/{taskId}/files` — via `fetchTaskFiles(taskId)` in
  `dashboard/src/api/taskFiles.ts`. Returns `TaskFilesResponse`:
  `{ success, files: TaskFileEntry[], base, workspace_path, reason? }`.
- `GET /api/tasks/{taskId}/file?path=<path>` — via
  `fetchTaskFileText(taskId, path)` in the same module. Returns
  `{ text, status }`, already normalized for the binary/403/404/413 cases
  (see §5.3 and §8).

Both live outside the generated `@aq/ts-client` per that file's own header
comment (the `/file` endpoint returns raw `text/plain`, not modeled in the
OpenAPI spec) — this view imports them the same way `TaskFilesPanel.tsx`
does, via `legacyFetch`-backed functions, not a new client call. No new API
surface for this view.

### 7.1 React Query hooks

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
```

Same query keys as `TaskFilesPanel.tsx` (`["taskFiles", taskId]` /
`["taskFile", taskId, selected]`) — deliberately. If the pane view and the
full-page route are ever open at once (pane persists across routes per
§5.3 / shell spec §5.3, so a user could have the pane open on task A while
routed to `/tasks/A/files` directly), React Query dedupes them as the same
cache entry instead of double-fetching. No new invalidation wiring needed
— the existing `refetchInterval: 5000` on the file list already keeps both
surfaces current, and `Refresh` (§6.1) triggers an immediate refetch
on-demand.

### 7.2 `filePath` arg → initial selection

On mount (and on `args.filePath` change via `setArgs`), if
`args.filePath` is set and present in `filesQ.data.files`, seed `selected`
to it:

```ts
useEffect(() => {
  if (!args.filePath) return;
  if (!filesQ.data?.files.some((f) => f.path === args.filePath)) return;
  setSelected(args.filePath);
}, [args.filePath, filesQ.data]);
```

Selecting a file from the list (§5.2 row click / `Enter`) calls
`setArgs({ ...args, filePath: f.path })` in addition to local `setSelected`
state, so the pane's args stay in sync with what's displayed — this is the
same pattern the plugin-interface spec describes for the file-browser view
(§5.3, "file browser starts at `{ workspace_id, path: 'src/' }`... updates
its own args"). Keeping `filePath` in sync means an agent-push that later
re-opens this exact view (or a user bookmarking/copy-pasting a palette
action) reproduces the same selection.

## 8. Loading + error + edge cases

All four states below are handled by branching on `filesQ` before
rendering the two-column body — same branch order as `TaskFilesPanel.tsx`,
reused directly (not reimplemented):

1. **Loading** (`filesQ.isLoading`) — `"Loading files…"` placeholder,
   `text-sm text-gray-500`.
2. **Error** (`filesQ.error`) — `"Failed to load files: <message>"`,
   `text-sm text-red-400`.
3. **`reason: "no_workspace"`** — `"Task has no attached workspace. Files
   will appear once the task acquires a worktree."` No file list rendered.
4. **`reason: "not_a_git_checkout"`** — `"Task workspace (<workspace_path>)
   is not a git checkout."` No file list rendered.
5. **Empty diff** (`files.length === 0`, no `reason`) — `"No changes vs
   <base> yet."`
6. **Individual file states** (right column, once a file is selected):
   - Loading → `"Loading <path>…"`.
   - Error → red error text.
   - **Binary** — `fetchTaskFileText` already collapses the `{reason:
     "binary", size}` JSON body into display text `"(binary file omitted
     (<N> KB))"` (see `taskFiles.ts` lines 46–58); this view renders it
     through the plain `<pre>` branch like any other non-markdown text.
   - **403 / 404 / 413** — likewise pre-normalized by `fetchTaskFileText`
     into `"(forbidden path)"` / `"(file not found)"` / `"(file exceeds
     512 KB cap)"`; rendered through the same `<pre>` branch. This view
     does not need its own status-code handling — it's centralized in the
     shared fetch function both consumers call, so a fix there
     automatically benefits both.
7. **Stale `filePath` arg** — handled in §7.2: if the arg doesn't match any
   current file, no selection is made and the view shows the "no selection"
   placeholder rather than erroring.

No new 403-at-the-view-level handling is needed beyond what's listed:
`filesQ`'s top-level fetch failing with a 403 (e.g. task belongs to a
project outside the caller's scope) surfaces through the generic
`filesQ.error` branch (#2) — `legacyFetch`'s throwing behavior on non-2xx
(per `dashboard/CLAUDE.md`) means this is indistinguishable from any other
fetch failure, which matches `TaskFilesPanel.tsx`'s existing behavior.

## 9. Agent-push examples

Per plugin-interface spec §6.5, the supervisor emits a `pane_open` frame.
Concrete examples for this view:

**Basic — review a task's changes:**
```
aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "opened the diff for task abc-123 →" \
    --pane-open '{"view": "diff-review-changes", "args": {"taskId": "abc-123"}}'
```

**With a pre-selected file (e.g. supervisor flags a specific file worth a
second look):**
```
aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "opened the diff for task abc-123 — check src/api/scope.py →" \
    --pane-open '{"view": "diff-review-changes", "args": {"taskId": "abc-123", "filePath": "src/api/scope.py"}}'
```

Both are `agent_pushable: true` (manifest §3), so the client auto-opens the
pane on arrival (subject to `to_kind === "user"`, per plugin-interface spec
§6.5) in addition to rendering the inline chip in the chat transcript.

Server-side mirror entry (plugin-interface spec §7, option A):

```python
# src/panes/registry.py
SERVER_PANE_REGISTRY = {
    ...,
    "diff-review-changes": {"agent_pushable": True},
}
```

## 10. Tests

`dashboard/src/panes/diff-review-changes/__tests__/index.test.tsx`:

**Manifest tests:**
- `manifest.id === "diff-review-changes"` (matches directory name).
- `diffReviewChangesArgsSchema` accepts `{ taskId: "t1" }` (minimal valid).
- `diffReviewChangesArgsSchema` accepts `{ taskId: "t1", base: "main",
  filePath: "a.ts" }` (full valid).
- `diffReviewChangesArgsSchema` rejects `{}` (missing `taskId`).
- `diffReviewChangesArgsSchema` rejects `{ taskId: "" }` (empty string).
- `manifest.open_shortcut === "$mod-shift-d"` is a valid normalized form
  (matches the shell's `$mod-<key>` / `$mod-shift-<key>` pattern).

**Component tests** (React Testing Library, `@tanstack/react-query`'s
`QueryClientProvider` wrapper + mocked `fetchTaskFiles`/`fetchTaskFileText`,
matching `TaskFilesPanel`'s existing test-setup conventions if any exist,
otherwise a fresh `QueryClient` per test):

- Renders the file list from a mocked `fetchTaskFiles` response (asserts
  each `path` appears, with correct status-letter coloring class).
- Clicking a file row calls `fetchTaskFileText` and renders its content in
  the preview column.
- Clicking a second file row switches the preview without leaving stale
  content from the first (asserts old content is gone, new content shown).
- `.md` file renders via `MarkdownPreview` (assert on a markdown-specific
  DOM artifact, e.g. a heading element from `remark-gfm` output) — vs a
  `.ts` file rendering via plain `<pre>`.
- Binary file (`fetchTaskFileText` mock resolves `{ text: "(binary file
  omitted (12 KB))", status: 200 }`) renders that literal string in the
  `<pre>` branch, not `MarkdownPreview`.
- `reason: "no_workspace"` response renders the no-workspace message and
  no file list.
- `reason: "not_a_git_checkout"` response renders that message with
  `workspace_path` interpolated.
- `filesQ` error (rejected mock) renders the error branch with the
  thrown message.
- `args.filePath` matching an entry in the mocked file list pre-selects it
  on mount (preview shows that file's content without a click).
- `args.filePath` NOT matching any entry leaves no selection (placeholder
  shown, no crash).
- `↑`/`↓`/`Enter` keyboard sequence moves focus and opens the focused file
  (simulated via `fireEvent.keyDown` on the pane container, given the
  registered `setShortcuts` bindings fire only while the pane holds focus —
  test harness renders the component with focus assumed, per how other
  pane-scoped-shortcut tests in the codebase are expected to stub
  `setShortcuts`/focus).
- `/` shortcut focuses the filter input (`document.activeElement` check).
- Typing in the filter input narrows the rendered file list to matching
  paths (case-insensitive substring).
- `close` prop: not directly exercised by this view (no explicit close
  button beyond the shell-provided header `×`), so this view's tests don't
  need a close-affordance test beyond confirming the prop is accepted
  without being called spuriously.
- Toolbar actions are invocable: `Refresh` action calls
  `filesQ.refetch`-equivalent (assert `fetchTaskFiles` called again);
  `Copy file path` calls `navigator.clipboard.writeText` with the selected
  path (and is `disabled` when nothing is selected); `Open full-page view`
  calls the mocked `useNavigate` with `/tasks/<taskId>/files`.
- Narrow-pane collapse: rendering with a mocked container width <400px
  (via mocked `ResizeObserver` or a `narrow`-forcing test hook) renders the
  stacked layout (assert list container has the stacked class / DOM order
  changes) vs the two-column layout at default width.

## 11. Implementation checklist

- [ ] Create `dashboard/src/panes/diff-review-changes/` directory.
- [ ] Write `manifest.ts` per §3 (import shared `PaneManifest` type from
      `dashboard/src/panes/types.ts` — create that shared file if it
      doesn't exist yet, since no pane view has landed before this one;
      keep it minimal, just the type from plugin-interface spec §4 with
      `icon` typed as the heroicons component type per §3's note).
- [ ] Promote `TaskFilesPanel.tsx`'s inline `statusColor` function to a
      named export (or move to a tiny shared `dashboard/src/lib/taskFiles.ts`
      helper) so this view imports it instead of duplicating the switch.
- [ ] Write `index.tsx` implementing the component contract: two-column /
      narrow-collapse layout (§5), toolbar (§6.1), pane-scoped shortcuts
      (§6.2), the two React Query hooks (§7.1), `filePath` arg sync (§7.2),
      and all loading/error/edge-case branches (§8).
- [ ] Add `"diff-review-changes": {"agent_pushable": True}` to
      `src/panes/registry.py` (server-side mirror, plugin-interface spec
      §7).
- [ ] Write `__tests__/index.test.tsx` per §10.
- [ ] Once `dashboard/src/panes/registry.ts` (the frontend registry,
      plugin-interface spec §4.1) exists, confirm this view's entry
      resolves and its `open_shortcut` doesn't collide with any other
      view's `open_shortcut` or a reserved shell shortcut (`$mod-K`,
      `$mod-\`, etc.) — run the shared registry test
      (`dashboard/src/panes/__tests__/registry.test.ts`, plugin-interface
      spec §9.2) once it exists.
- [ ] Run `tests/test_pane_registry_parity.py` (plugin-interface spec §7)
      once the parity test exists, to confirm the frontend and backend
      registries agree on this view's id.

## 12. Open questions

- **`base` arg override has no backend wiring yet.** `GET
  /api/tasks/{id}/files` doesn't accept a `base` query param today (see
  §4). This view accepts and displays the arg but can't act on it without
  a backend change (`src/api/routes` — outside this spec's scope). If a
  supervisor or palette action passes a `base` that doesn't match the
  daemon's own computed base, the view should surface both (its own arg
  value and the daemon's `data.base`) rather than silently picking one —
  exact copy TBD at implementation time.
- **Full-page navigation loses the current selection.** §5.4 notes
  `TaskFiles.tsx` / `TaskFilesPanel` don't accept an initial-selection prop
  or URL param today. A small follow-up (`?file=<path>` query param read by
  `TaskFiles.tsx`, or an initial-selection prop on `TaskFilesPanel`) would
  make the pane-to-full-page handoff lossless. Not blocking v1.
- **Container-width measurement vs a shell-supplied width prop.** §5.1
  notes this view measures its own container via `ResizeObserver` because
  the plugin-interface spec's `PaneViewProps` doesn't currently pass the
  pane's current width down to the view. If a later shell revision adds
  that (natural, since the shell already tracks `width` in `PaneState`),
  this view should switch to the prop and drop its own observer — flagged
  here so whoever lands that shell change knows there's a consumer wanting
  it.
- **No real diff hunks.** As noted in §2, this is whole-file preview, not
  a true unified diff. If a future backend endpoint adds hunk-level diff
  data, this view is the natural place to add a diff-marker gutter
  (+/− line prefixes) — deferred, not designed here.
