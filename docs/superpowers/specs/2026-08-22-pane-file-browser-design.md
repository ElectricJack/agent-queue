# Pane View: `file-browser` — Design

**Status:** design (approved by brainstorming pass 2026-08-22).
**Depends on:** `2026-08-22-dashboard-shell-v2-design.md` (shell
primitives), `2026-08-22-pane-plugin-interface-design.md` (pane
contract — every section below implements that contract for this one
view).
**Ship priority:** v1 (Phase C first wave, with `task-detail` and
`diff-review-changes`).

## 1. Goal

General-purpose file browsing bound to a workspace: navigate a
directory tree and preview file contents, independent of any task.
Unlike `diff-review-changes` (task-scoped, shows what changed) or
`task-detail` (task-scoped, task metadata), `file-browser` is
workspace-scoped and shows the tree as it stands, at any path.

## 2. Non-goals

- Not a code editor — read-only, no save/edit/delete/upload.
- Not a diff viewer — no change markers. `diff-review-changes` covers
  "what changed."
- Not full-text/content search — the toolbar filter matches entry
  names in the current directory only, no cross-tree grep.
- Not multi-workspace in one pane instance — a workspace change is a
  new `open()`/`setArgs` call, treated as a full reset (§5.4).

## 3. Manifest

```ts
// dashboard/src/panes/file-browser/manifest.ts
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

Directory: `dashboard/src/panes/file-browser/`. `manifest.id` matches
the directory name per the plugin spec's registry validation (§4.2).

## 4. Args + validation

`{ workspaceId: string, path?: string }`.

- `workspaceId` — required non-empty string, resolved server-side to
  a `Workspace` row (§8). Not validated client-side beyond shape.
- `path` — optional, defaults to `""` (workspace root) via zod
  `.default("")`. No client-side traversal validation — a malformed
  `path` is handled by the backend's canonicalization + containment
  check (§8.2) and surfaces as an error state (§9), not a zod error.

Missing/invalid `workspaceId` fails per plugin spec §6.1: `open()`
logs a console error and no-ops.

## 5. Component

### 5.1 Layout

Two-column when the pane is `>= 480px` wide (the shell's default open
width); stacked below that (dragged-narrow or mobile bottom sheet).
Breakpoint is a `ResizeObserver` on the pane's own container, not a
viewport media query, since pane width changes independently via the
resize divider (shell spec §5.4).

```
Wide:                              Narrow:
┌────────┬─────────────┐           ┌─────────────────┐
│ tree    │ preview      │           │ tree (stacked)   │
│ ~50%    │ ~50%         │           ├─────────────────┤
│         │              │           │ preview          │
└────────┴─────────────┘           └─────────────────┘
```

### 5.2 Tree pane

Lists directory entries at the current `path`. Each row: folder/file
icon (symlinks get a small badge, `is_symlink: true`), name, and size
(files only, human-formatted; directories show no size — no recursive
computation, §8.2). Directories sort above files, alphabetical within
each group (backend does the sort, §8.2, so rendering is order-stable
across refetches).

**Breadcrumb** above the tree: `workspace root / dir / subdir`, each
segment clickable via `setArgs({ workspaceId, path })`.

**Selection:**
- Directory row (click / `Enter`) → `setArgs({ workspaceId, path:
  <dir path> })`. Preview pane is left untouched — browsing shouldn't
  blank what the user is reading (see §5.4).
- File row (click / `Enter`) → sets local `previewPath` state (not
  part of pane `args` — see §5.4 for the split).

### 5.3 Preview pane

- Empty: "Select a file to preview" (no `previewPath` set).
- Loaded: filename header + monospace `<pre>` block. No syntax
  highlighting in v1 (open question, §13).
- Binary (`{ reason: "binary" }`, §8.3): "(binary file, size: N KB) —
  preview not available".
- Oversized (`413`, cap 512 KB, §8.3): "File too large to preview
  (over 512 KB)".
- Preview scrolls independently of the tree column.

### 5.4 setArgs vs local state

`path` (directory position) goes through `setArgs` — it's shell-
addressable navigation a palette re-open should be able to restore
(plugin spec §5.3). The selected preview file is transient browse
state: it lives in `useState`, is NOT reset when `path` changes
(directory navigation shouldn't blank an open preview), and IS reset
(to `null`, along with the filter, §5.5) when `args.workspaceId`
changes — a workspace switch is a different tree entirely.

### 5.5 Filter

`/` focuses a text input above the tree that filters the current
directory's entries by case-insensitive substring match on name.
Client-side only, no backend round-trip. Clears on `Esc` (shell
list-motion convention, §8.4 of the shell spec) or automatically when
navigating to a new directory.

## 6. Toolbar + shortcuts

### 6.1 Toolbar (`setToolbar`)

`[Refresh] [Copy path] [Up one dir] [Open workspace root]`

- **Refresh** — invalidates + refetches the current directory's React
  Query key (§7.1) and the preview query if a file is selected.
- **Copy path** — copies the previewed file's path if one is
  selected, else the current directory's path, via
  `navigator.clipboard.writeText`.
- **Up one dir** — `setArgs({ workspaceId, path: parent(path) })`;
  disabled at `path === ""`.
- **Open workspace root** — `setArgs({ workspaceId, path: "" })`;
  disabled at `path === ""`.

### 6.2 Shortcuts (`setShortcuts` + list motion)

| Key | Action |
|---|---|
| `↑` `↓` | Move tree selection (component-local list motion, not `setShortcuts` — mirrors shell spec §8.4's always-on list grammar). |
| `Enter` | Open focused entry — dir navigates, file previews. |
| `Backspace` | Up one dir (no-op at root). Registered via `setShortcuts`. |
| `/` | Focus filter input. Registered via `setShortcuts`. |
| `r` | Refresh. Registered via `setShortcuts`. |

All bindings fire only while the pane holds focus (plugin spec §5.2)
and appear in the cheat sheet under "File Browser" (shell spec §8.8).

## 7. Data + queries

React Query, consistent with the dashboard's existing data layer.

```ts
function useWorkspaceBrowse(workspaceId: string, path: string) {
  return useQuery({
    queryKey: ["workspace-browse", workspaceId, path],
    queryFn: () => fetchWorkspaceBrowse(workspaceId, path),
    staleTime: 10_000,
  });
}

function useWorkspaceFile(workspaceId: string, path: string | null) {
  return useQuery({
    queryKey: ["workspace-file", workspaceId, path],
    queryFn: () => fetchWorkspaceFile(workspaceId, path!),
    enabled: path != null,
    staleTime: 10_000,
  });
}
```

`fetchWorkspaceBrowse`/`fetchWorkspaceFile` call the endpoints in §8,
via whatever `apiFetch`/`fetchJson` wrapper the dashboard's data layer
already uses (no new HTTP client). Both live in
`dashboard/src/panes/file-browser/hooks.ts` (plugin spec §3). Query
key includes `path`, so back-and-forth navigation is served from
cache (subject to `staleTime`); `Refresh` bypasses via
`queryClient.invalidateQueries`.

## 8. Backend requirements

**New backend work — these endpoints don't exist today.** The
existing `/api/tasks/{task_id}/files` + `/file` pair
(`src/api/task_files.py`) only resolves a workspace *through a task*,
and only ever lists "files changed in a diff," never a plain
directory. This view needs a workspace resolved directly by id, plus
directory listing.

### 8.1 New router: `src/api/workspace_files.py`

```
GET /api/workspaces/{workspace_id}/browse?path=<relpath>
GET /api/workspaces/{workspace_id}/file?path=<relpath>
```

Registered in `src/api/app.py` next to the existing task-files router
(near the `# Task file preview (Phase 5)` comment at line 113).

### 8.2 `browse`

Params: `path`, optional, defaults to `""` — same default as the
frontend args schema, kept in sync deliberately.

1. `orch.db.get_workspace(workspace_id)` — not found → `404`.
2. Scope check (§8.4) before touching the filesystem.
3. If `workspace.workspace_path` is falsy → `200` with `{ path,
   entries: [], reason: "no_workspace_path" }` (mirrors the existing
   `reason: "no_workspace"` convention in `task_files.py` lines
   90-98, so the frontend's reason-based empty state pattern
   extends naturally).
4. **Path safety — identical algorithm to the existing
   `/api/tasks/{id}/file` endpoint** (`task_files.py` lines 165-202):
   reject absolute paths; lexical-resolve to catch `..` traversal
   (403, even for a non-existing target — an escape attempt, not a
   404); strict-resolve to follow symlinks and confirm containment
   (403 on escape, 404 on missing). Additionally require the
   resolved path to be a directory — `404 "not a directory"`
   otherwise.
5. List entries (`os.scandir` or `Path.iterdir` + `stat`):
   - `name`, `type` (`"dir"`/`"file"` — a symlink is classified by
     its *resolved* target; a broken symlink is omitted from the
     listing rather than erroring the whole response).
   - `size` — files only.
   - `is_symlink` — `true` if the directory entry itself is a
     symlink.
   - Dotfiles (`.git`, `.env`, etc.) are **not** filtered in v1 (open
     question, §13).
6. Sort: directories first, then files, alphabetical, case-
   insensitive — backend does this so the frontend needs no
   client-side sort.

Response:

```json
{
  "success": true,
  "path": "src/api",
  "entries": [
    { "name": "handlers", "type": "dir" },
    { "name": "app.py", "type": "file", "size": 4213 },
    { "name": "scope.py", "type": "file", "size": 1876, "is_symlink": true }
  ]
}
```

`path` echoes the canonicalized relative path.

### 8.3 `file`

Params: `path`, required (`Query(...)`).

**Reuse the existing task-file handler's logic**, don't reimplement
it. Extract `task_files.py`'s `get_file` body (lines 165-242 — path
safety through the binary-heuristic response) into a shared helper:

```python
# src/api/file_serving.py (new, shared)
async def serve_workspace_relative_file(
    workspace_path: str, path: str
) -> PlainTextResponse | JSONResponse:
    """Path-safe, size-capped, binary-aware file read. Shared by
    /api/tasks/{id}/file and /api/workspaces/{id}/file — both resolve
    to "a workspace root + a relative path" by the time they get
    here."""
```

Both routers become thin wrappers: resolve `task_id` →
`workspace.workspace_path` (existing) or `workspace_id` →
`workspace.workspace_path` (new), then delegate. This is a refactor
of existing code — call it out explicitly in the PR; any existing
tests for the task-file endpoint must keep passing unmodified.

Response — identical to the existing task-file endpoint: `text/plain;
charset=utf-8` for text; `{ success: true, reason: "binary", size,
path }` JSON for binary (NUL byte in first 8 KiB); `413` over 512 KB;
`403`/`404` per the path-safety cases in §8.2 step 4 (same shared
function).

### 8.4 Scope check — both endpoints

Both endpoints scope-check before filesystem work: the session's
`RequestScope` must be able to see the workspace's project. This is
the existing scope model (`src/api/scope.py`'s elevated/project-match
logic), applied at the HTTP layer via a new small guard:

```python
def _require_workspace_scope(request, workspace) -> None:
    """403/404 if the caller's RequestScope can't see workspace.project_id."""
```

- Resolve `workspace.project_id` (already fetched in §8.2 step 1).
- A session scoped to project A gets `404` (not 403 — avoid leaking
  workspace existence, matching the task-files endpoint's posture;
  confirm this against actual precedent elsewhere in `src/api/`
  before implementing, see §13) when the workspace belongs to a
  different project.
- Global-admin scope (dashboard shell v2 spec §4.2 — `elevated` +
  `project_id is None`) sees every workspace.
- Follow whatever pattern `src/api/dependencies.py` already uses to
  pull `RequestScope` off the request in existing routers — don't
  invent a new extraction mechanism.

### 8.5 Tests

`tests/test_workspace_files_api.py` (new; mirror
`tests/test_task_files_api.py`'s fixture/harness pattern if it
exists):

- `browse` root lists entries, dirs before files, alphabetical.
- `browse` with `path=<subdir>` lists that subdir.
- `browse` with `path=../../etc` → `403`.
- `browse` through a symlink escaping the workspace → `403`.
- `browse` with `path` pointing at a file → `404 "not a directory"`.
- `browse` on unknown `workspace_id` → `404`.
- `browse` on a workspace with no `workspace_path` → `200`,
  `reason: "no_workspace_path"`, empty entries.
- `browse`/`file` from a session scoped to a different project → `404`.
- `browse`/`file` from a global-admin session → succeeds regardless
  of the workspace's project.
- `file` on a text file → `200` body matches contents.
- `file` on a binary file (NUL in first 8KB) → `200` JSON
  `{ reason: "binary" }`.
- `file` over the 512KB cap → `413`.
- `file` with absolute path / `..` traversal → `403`.
- Regression: existing task-file endpoint tests still pass unmodified
  after the `serve_workspace_relative_file` extraction.

## 9. Loading + error + edge cases

| Case | Behavior |
|---|---|
| Directory query loading | Tree shows skeleton rows; preview unaffected. |
| Directory query error (network/5xx) | Inline error + Retry (same handler as Refresh). |
| `404` workspace not found | Tree: "Workspace not found." |
| `404` out of scope | Same rendering as not-found (deliberate — §8.4's "don't leak existence" posture). |
| `reason: "no_workspace_path"` | Tree: "This workspace has no filesystem path yet — nothing to browse." (mirrors `task_files.py`'s "no worktree attached" copy). |
| Empty directory | Tree: "This directory is empty." |
| File preview loading | Skeleton in preview; filename header renders immediately (known from `previewPath`). |
| File preview error | Inline error + Retry. |
| Filter, zero matches | "No files match “<query>”." below the empty list; input stays focused. |
| Workspace changes (`setArgs` new `workspaceId`) | Full reset (§5.4): `previewPath` cleared, filter cleared, tree scrolled to top. |
| Rapid navigation | React Query's per-key cache + `staleTime` coalesces naturally; no debounce needed (distinct query keys per directory, not search-as-you-type). |

## 10. Agent-push examples

Per plugin spec §6.5. Point at a directory:

```
aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "here's the workspace layout →" \
    --pane-open '{"view": "file-browser", "args": {"workspaceId": "ws_abc123", "path": "src/api"}}'
```

Point at a specific file:

```
aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "check out this config file →" \
    --pane-open '{"view": "file-browser", "args": {"workspaceId": "ws_abc123", "path": "config/settings.yaml"}}'
```

For a file `path` (not a directory), the args contract stays simple
(`path` is always "where the tree is rooted," §4) — the view handles
the fallback itself on mount: one `browse` call for `args.path`; if
it 404s `"not a directory"`, retry against the parent
(`path.split("/").slice(0,-1).join("/")`) and set `previewPath` to
the original `args.path` once that succeeds. No manifest/args-schema
change needed.

Since `agent_pushable: true`, `useChatTranscript` auto-dispatches
`pane.open("file-browser", args)` on arrival and renders the
`InlineEventCard` `pane_open` chip (plugin spec §6.5).

## 11. Tests

`dashboard/src/panes/file-browser/__tests__/index.test.tsx`:

**Manifest** (plugin spec §9.1):
- `manifest.id === "file-browser"` matches directory name.
- `fileBrowserArgsSchema` accepts `{ workspaceId: "x" }` (path
  defaults `""`) and `{ workspaceId: "x", path: "a/b" }`; rejects `{}`
  and `{ workspaceId: 5 }`.
- `open_shortcut` (`"$mod-shift-f"`) is a valid normalized form.

**Component:**
- Renders tree from mocked `browse` response (dirs before files,
  correct sizes/icons).
- Directory click calls `setArgs` with new `path`, same `workspaceId`.
- File click sets `previewPath` (preview fetches/renders that file)
  without calling `setArgs`.
- Preview switching: select file A → renders A; select file B without
  navigating → renders B, A gone.
- Up-navigation: disabled at root; at `path: "a/b"` calls `setArgs`
  with `path: "a"`.
- Breadcrumb segment click calls `setArgs` with that path; root
  clears to `""`.
- `close` prop fires from the header `×`.
- `setToolbar` called with all four actions on mount; `Refresh`
  triggers `queryClient.invalidateQueries` with the right key.
- `setShortcuts` called with `Backspace`/`/`/`r`; firing each produces
  the expected effect.
- Empty directory → "This directory is empty."
- `reason: "no_workspace_path"` → correct message, no crash.
- Binary file → binary message rendered instead of raw content.
- Filter hides non-matching rows client-side (no new network call).
- Mount-time file-push fallback (§10): first `browse` 404s "not a
  directory", second (parent) succeeds, parent renders with the file
  auto-selected into preview.

## 12. Implementation checklist

### Frontend

- [ ] `dashboard/src/panes/file-browser/manifest.ts` (§3).
- [ ] `dashboard/src/panes/file-browser/hooks.ts` — query hooks + fetch
      helpers (§7).
- [ ] `dashboard/src/panes/file-browser/index.tsx` — layout (§5.1),
      tree + breadcrumb (§5.2), preview (§5.3), filter (§5.5), toolbar
      (§6.1), shortcuts (§6.2), mount-time file-push fallback (§10).
- [ ] `dashboard/src/panes/file-browser/__tests__/index.test.tsx` (§11).
- [ ] Confirm `file-browser` is picked up by
      `dashboard/src/panes/registry.ts`'s `import.meta.glob` (plugin
      spec §4.1) — no manual registration expected, but verify.
- [ ] Run `dashboard/src/panes/__tests__/registry.test.ts` — no
      `open_shortcut` collision, id unique.

### Backend

- [ ] `src/api/file_serving.py` (new) — extract
      `serve_workspace_relative_file` from `task_files.py::get_file`
      (§8.3), behavior-preserving.
- [ ] `src/api/task_files.py` — `get_file` delegates to the shared
      helper; existing tests still pass unmodified.
- [ ] `src/api/workspace_files.py` (new) — `build_workspace_files_router()`
      with `browse` (§8.2) and `file` (§8.3), plus
      `_require_workspace_scope` (§8.4).
- [ ] `src/api/app.py` — register the new router near line 113.
- [ ] `tests/test_workspace_files_api.py` (new, §8.5).
- [ ] `src/panes/registry.py` (server-side pane mirror, plugin spec
      §7) — add `"file-browser": {"agent_pushable": True}`.
- [ ] Run `tests/test_pane_registry_parity.py` — frontend/backend
      view-id parity after adding both entries.

## 13. Open questions

- **Dotfile visibility.** v1 lists `.git`, `.env`, etc. unfiltered
  (§8.2 step 5). A `.git` directory's `objects/` fan-out is rarely
  useful to browse. Consider hiding top-level `.git` or a "show
  hidden" toggle in a follow-up — bundled with the pagination
  question below since both are "large directory" concerns.
- **No pagination on `browse`.** Thousands of entries (e.g.
  `node_modules/.bin`) return in one response. Acceptable for v1;
  `limit`/`cursor` is the natural extension if this view gets used
  against JS-heavy repos, but it changes the response shape — not
  spec'd here.
- **404 vs 403 for out-of-scope workspace access (§8.4).** Spec'd as
  404 to avoid leaking existence, but confirm against actual
  precedent in `src/api/scope.py` / other HTTP-layer scope guards
  before implementing — follow the codebase's existing convention if
  it differs.
- **Syntax highlighting.** Deferred (§5.3) — plain monospace in v1. A
  future highlighter swap is a pure `index.tsx` change, no backend or
  manifest impact.
- **Large-file "load more."** The 512 KB hard cap (inherited via the
  shared helper, §8.3) could become truncated-preview-with-load-more,
  but that needs an offset/range parameter — not in v1.
