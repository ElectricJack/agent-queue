# Pane View: `task-detail` — Design

**Status:** design (approved by brainstorming pass 2026-08-22).
**Depends on:**
- `2026-08-22-dashboard-shell-v2-design.md` (shell primitives, §5 `<ShellPane>`)
- `2026-08-22-pane-plugin-interface-design.md` (contract this view implements)

**Ship priority:** v1 (Phase C, alongside `diff-review-changes` and `file-browser`).

## 1. Goal

Give the shell pane a `task-detail` view showing everything a user needs
about one task, without leaving the graph, a table, or a chat. It
replaces two existing pieces of UI:

- `dashboard/src/pages/command-center/TaskSidebar.tsx` (266 lines) — the
  inline sidebar the Command Center graph renders on node click. Per
  shell spec §7.4, the graph stops rendering this inline and instead
  calls `pane.open("task-detail", { taskId })`.
- The read-only "Details" tab content of `dashboard/src/pages/TaskDetail.tsx`
  (568 lines) — this view is a pane-sized projection of the same
  read-only sections `TaskSidebar` already shows, sourced from the same
  `useTask` data.

Task inspection is the most common "I clicked something, now what"
interaction across the dashboard (graph nodes, table rows, agent rows,
chat mentions) — v1 priority follows directly from that.

## 2. Non-goals

- Not replacing `/tasks/:id`. Editing, the Explain tab, and the Graph
  tab stay full-page-only; "edit" always routes to the full page.
- Not changing `TaskActions.tsx` or its mutation hooks — this view is a
  new consumer, not a new implementation.
- Not building the `InlineEventCard` `pane_open` chip — shared shell
  work (interface spec §6.5), done once regardless of which view ships
  first.
- Not wiring the Command Center graph's node-click handler to
  `pane.open` — that's Command Center consolidation (shell spec Phase
  D), a separate PR that depends on this view but isn't part of it.
- Not adding a task entity picker for the palette action (§6).

## 3. Manifest

```ts
// dashboard/src/panes/task-detail/manifest.ts

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
  // No open_shortcut — reached via click-through, the palette action
  // below, or agent push. Keeps keyboard slots free for less common
  // views.
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Open task",
  palette_section: "Task",
};
```

- `id` matches the directory name (registry validation, interface spec
  §4.2).
- `route_scope: "cross-route"` (the default) — nothing here reads route
  params; a user should keep the pane open while navigating elsewhere.
- `agent_pushable: true` — this is the view supervisors push most often
  when referencing a task.
- `palette_label`/`palette_section` override the interface spec's
  `"Panes"` default per the task brief.

## 4. Args + validation

```ts
{ taskId: string }
```

- `taskId` must be non-empty (`z.string().min(1)`). Empty string is the
  realistic bad-args case; it fails loudly at `open()` (interface spec
  §6.1) instead of rendering a blank pane.
- No further shape validation. A well-formed but unknown id is a normal
  "not found" render (§8), not a manifest-schema failure.
- `setArgs` is unused by this view. Clicking a related task (subtask,
  depends-on, blocks, parent) calls `open` again with the new id — a
  navigation between distinct tasks, not a refinement of the same one
  (contrast the file-browser view's `setArgs` use for drilling into a
  subdirectory of the *same* session).

## 5. Component

File: `dashboard/src/panes/task-detail/index.tsx`, default export
`TaskDetailPane`, typed against `PaneViewProps<TaskDetailArgs>`.

### 5.1 Data

```ts
const { data: task, isLoading, isError } = useTask(args.taskId);
const { data: gates } = useGates({ projectId: task?.project_id });
```

Both from `dashboard/src/api/hooks.ts` (§7 covers filtering + invalidation).

### 5.2 Sections, top to bottom

Same set `TaskSidebar.tsx` shows today, rendered at the pane's ~480px
default width (narrower than `TaskSidebar`'s 640/720px — the metadata
grid collapses to one column below ~400px, same breakpoint pattern
`TaskSidebar` already uses).

1. **Title block** — monospace task id, `<h2>` title (or "Loading…"),
   badge row: `StatusBadge`, project id, `P{priority}`, `task_type`,
   `profile_id`, `intelligence_class` pill, `subtask` pill if
   `is_plan_subtask`. Matches `TaskSidebar.tsx` lines 31–61. (The shell
   header already shows `[icon] Task` per interface spec §5 — this
   block is the task's own identity, not duplicate chrome.)
2. **Actions bar** — `<TaskActions task={task} />` once loaded. Same
   component `TaskDetail.tsx`/`TaskSidebar.tsx` already use; renders
   `null` when no status-appropriate action exists.
3. **Description** — shown only if non-empty, `whitespace-pre-wrap`
   block (`TaskSidebar.tsx` lines 76–85 styling).
4. **Metadata grid** ("Details"): Agent, Retries (`retry_count /
   max_retries`), Requires approval, Auto-approve plan, Skip
   verification, Branch (monospace), Created/Updated (formatted like
   `TaskSidebar`'s `formatDate`), Parent task (link → `open("task-detail",
   { taskId: task.parent_task_id })`, not a router `<Link>`; see §5.4).
5. **Pull request** — shown only if `task.pr_url` set; external link,
   `target="_blank"`, `ArrowTopRightOnSquareIcon`.
6. **Gates** — shown only if a gate's `task_ids` includes `args.taskId`
   (§7.2 type note). `human`+`open` gates get inline Approve/Reject
   wired to `useResolveGate()` directly — this view has no parent to
   delegate to (unlike `TaskSidebar`, which took an `onResolveGate`
   prop).
7. **Subtasks / Depends on / Blocks** — each shown only if non-empty.
   Row: title (click → `open("task-detail", { taskId: ref.id })`) +
   `StatusBadge`.

No "Open full task page →" footer link — that affordance moves to the
toolbar (`[Open full detail page]`, §6), visible without scrolling.

### 5.3 Width behavior

Standard shell resize (200–800px, default 480px). One width-responsive
rule: metadata grid drops from two columns to one below ~400px content
width; everything else already wraps.

### 5.4 Internal vs. page navigation

Related-task links (parent, subtasks, depends-on, blocks) call
`open("task-detail", { taskId })` — pane-internal, replaces current
content (interface spec §10: v1 nested-pane behavior). Back-navigation
is not modeled, same as the interface spec's open question and same as
today's `TaskSidebar` (its links leave the sidebar entirely). The
toolbar's `[Open full detail page]` is the only route-navigating
affordance in this view.

## 6. Toolbar + shortcuts

```ts
setToolbar([
  { id: "open-full", label: "Open full detail page", icon: ArrowTopRightOnSquareIcon,
    onClick: () => navigate(`/tasks/${args.taskId}`) },
  { id: "copy-id", label: "Copy id", icon: ClipboardIcon,
    onClick: () => navigator.clipboard.writeText(args.taskId) },
]);

setShortcuts([
  { key: "o", label: "Open full detail page", onFire: () => navigate(`/tasks/${args.taskId}`) },
  { key: "c", label: "Close task", onFire: () => setModal("close") },
  { key: "r", label: "Reopen with feedback", onFire: () => setModal("reopen") },
  { key: ".", label: "More actions", onFire: () => setMoreOpen(true) },
]);
```

`navigate` from `useNavigate()` — same pattern `TaskActions.tsx` already
uses (`navigate("/tasks")` after delete).

- `o` mirrors the toolbar's primary action, per shell spec §8.7's
  task-row/task-detail vocabulary.
- `c` opens a close-task confirmation modal. **No "close" concept
  exists today** — `TaskActions` has "Delete" (destructive, permanent)
  but nothing named "close." v1 maps `c` to the existing
  `useDeleteTask()` mutation and its confirm-modal copy, reusing what
  `TaskActions` already renders for "Delete." Flagged in §12 — the
  ambiguity originates in the shell spec (§8.7 names `c` = "close task"
  for table rows too, without defining it either).
- `r` opens the same reopen-with-feedback modal `TaskActions` already
  has (`useReopenWithFeedback`), gated the same way (only meaningful on
  `COMPLETED`/`FAILED`).
- `.` opens a dropdown of every registered action for the task. Since
  `TaskActions` doesn't expose its internal `visible` button list
  today, this view duplicates the same conditional set locally rather
  than extracting a shared hook (left as a follow-up, §12).

No `open_shortcut` in the manifest — these shortcuts only fire once the
pane is already open and focused.

## 7. Data + queries

### 7.1 Hooks

- `useTask(args.taskId)` (`dashboard/src/api/hooks.ts:276`) —
  `{ data, isLoading, isError }`, `refetchInterval: 60_000`. No new hook.
- `useGates({ projectId: task?.project_id })`
  (`dashboard/src/api/hooks.ts:1045`) — gate the call so it doesn't fire
  an all-projects fetch before `task` resolves (`enabled: !!task?.project_id`
  wrapper; the hook as written today doesn't take `enabled` itself, so
  this is a small local addition — see §12).
- `useResolveGate()` (`dashboard/src/api/hooks.ts:1081`) — used directly
  by the Gates section's Approve/Reject buttons. Already invalidates
  `["gates"]`, `["gate", id]`, `["tasks"]` on success — no extra
  invalidation needed here.
- `<TaskActions task={task} />` — internally owns `useStopTask`,
  `useRestartTask`, `useSkipTask`, `useApproveTask`, `useApprovePlan`,
  `useRejectPlan`, `useDeletePlan`, `useReopenWithFeedback`,
  `useDeleteTask`, `useProvideInput`. This view calls none of these
  directly except via the `r`/`c` shortcuts opening `TaskActions`'s own
  modal flows.

### 7.2 Gate `task_ids` — a type gap to flag

`TaskSidebar.tsx` filters gates via `(g.task_ids ?? []).includes(taskId)`
against `GraphGate` (`packages/aq-ts-client/src/types.gen.ts:1978`),
which declares `task_ids?: Array<string>`. `useGates()` instead returns
`GateSummary[]` (`types.gen.ts:1223`), which has **no typed `task_ids`**
— only an index signature (`[key: string]: unknown | string`). The
daemon's `gate_list` command returns `task_ids` on each row in practice
(same underlying gate row `GraphGate` projects from); this is a
generated-types gap, not a missing backend field.

This view filters with a local cast:
`(gates as Array<GateSummary & { task_ids?: string[] }>).filter(g => (g.task_ids ?? []).includes(args.taskId))`.
Regenerating the OpenAPI types properly (adding `task_ids` to the
`GateSummary` Pydantic response model, per the dashboard's own
`CLAUDE.md` convention) is out of scope here — flagged, not blocking.

### 7.3 Invalidation

No new rules. `useTask` and `useGates` already refetch on their own
intervals and are already invalidated by every mutation that touches
them elsewhere in the app (`useEditTask`, `useResolveGate`, etc.).
`TaskActions`'s mutations are untouched by this view.

### 7.4 Agent-push refresh

A second `pane_open` for the same already-open `taskId` re-validates
args and re-sets pane state (interface spec §6.1) — a no-op
refetch-on-mount at worst via React Query's cache. No special handling
needed.

## 8. Loading + error states

Per interface spec §11 (no shared skeleton primitive; each view owns
its own):

- **Loading** (`isLoading`, no cached `task`): title reads "Loading…";
  every other section conditionally renders off `task?.field` and so
  degrades to empty automatically — matches `TaskSidebar`'s existing
  behavior (it doesn't guard `isLoading` separately).
- **Not found** (`isError`, or resolved with no task — `get_task` errors
  on unknown id rather than returning null, matching `TaskDetail.tsx`'s
  `if (!task) return <p>Task not found.</p>`): centered "Task not
  found." `[Copy id]` stays useful; `[Open full detail page]` still
  navigates (the full page has its own not-found fallback).
- **Gate fetch failure**: Gates section silently omits itself — same
  as "no gates," since gates are supplementary to the task's own data.

## 9. Agent-push examples

```json
{
  "message_id": "msg_01",
  "from_kind": "session",
  "from_id": "supervisor-global",
  "to_kind": "user",
  "to_id": "dashboard",
  "thread_id": "dashboard:global",
  "body_kind": "pane_open",
  "body": "Task xyz just moved to AWAITING_APPROVAL — opened it on the right →",
  "pane_open": { "view": "task-detail", "args": { "taskId": "xyz-789" } },
  "created_at": 1755878400
}
```

CLI form issuing that frame:

```
aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "Task xyz just moved to AWAITING_APPROVAL — opened it on the right →" \
    --pane-open '{"view": "task-detail", "args": {"taskId": "xyz-789"}}'
```

Per-project supervisor equivalent (different `from_id`/`thread_id`,
same `pane_open` shape):

```json
{
  "message_id": "msg_02",
  "from_kind": "session",
  "from_id": "supervisor-demo",
  "to_kind": "user",
  "to_id": "dashboard",
  "thread_id": "dashboard:demo",
  "body_kind": "pane_open",
  "body": "here's the task that's blocking the release →",
  "pane_open": { "view": "task-detail", "args": { "taskId": "abc-123" } },
  "created_at": 1755878460
}
```

Both validate against `taskDetailArgsSchema` (§3) and are accepted by
the server-side mirror (interface spec §7) once `src/panes/registry.py`
carries `"task-detail": {"agent_pushable": True}` (§11).

## 10. Tests

`dashboard/src/panes/task-detail/__tests__/index.test.tsx`

**Manifest tests:**
- `manifest.id === "task-detail"`.
- `taskDetailArgsSchema` accepts `{ taskId: "abc-123" }`; rejects
  `{ taskId: "" }` and `{}`.
- `manifest.open_shortcut` is `undefined` (asserted explicitly — its
  absence is deliberate, per §3).
- `manifest.agent_pushable === true`; `palette_label === "Open task"`;
  `palette_section === "Task"`.

**Component tests (the four required by the brief, plus contract
coverage from interface spec §9.1):**
- Renders with valid args — mount with `useTask`/`useGates` mocked
  (fixture task), no throw, title/badges/metadata grid present.
- "Open full detail page" navigates — capture the `setToolbar`
  registration, invoke `"open-full"`'s `onClick`, assert `navigate`
  called with `/tasks/t1` (mocked via `react-router-dom` test utils).
- "Copy id" writes to clipboard — stub
  `navigator.clipboard.writeText`, invoke `"copy-id"`, assert called
  with `"t1"`.
- Actions bar reflects task status — fixture at `AWAITING_APPROVAL`
  shows "Approve"; rerender with `COMPLETED` shows "Restart" and
  "Reopen with Feedback" instead (confirms the view passes the current
  `task` through, not a stale one — `TaskActions`'s own branching has
  its own coverage elsewhere).
- Gate Approve button calls `useResolveGate().mutate` with the gate id.
- Clicking a subtask/depends-on/blocks row calls the mocked
  `useShellPane().open` with `{ taskId: <ref.id> }`.
- Not-found state (`isError: true`) renders "Task not found" with
  `[Open full detail page]` still present.
- Loading state (`isLoading: true, data: undefined`) renders "Loading…"
  without crashing.
- `setShortcuts` was called with bindings keyed exactly `["o", "c", "r",
  "."]`; each `onFire` triggers its navigate/modal-open side effect.

## 11. Implementation checklist

- [ ] Create `dashboard/src/panes/task-detail/`.
- [ ] `manifest.ts` — contents as given in §3; export
      `taskDetailArgsSchema` for test reuse.
- [ ] `index.tsx` — `TaskDetailPane` per §5–§6. Imports `useTask`,
      `useGates`, `useResolveGate` from `../../api/hooks`; `TaskActions`
      from `../../components/TaskActions`; `StatusBadge` from
      `../../components/StatusBadge`; `useShellPane` from the shell
      store (path set by whatever Phase B lands as). Contains the §7.2
      gate-filtering cast as a local, unexported helper.
- [ ] `__tests__/index.test.tsx` — all tests from §10.
- [ ] Add `"task-detail": {"agent_pushable": True}` to
      `src/panes/registry.py` (server-side mirror, interface spec §7 —
      doesn't exist yet; whichever Phase C view lands first creates the
      file, this is a one-line add either way).
- [ ] Confirm `dashboard/src/panes/__tests__/registry.test.ts` passes
      (shared scaffold, interface spec §9.2 — created by the first
      Phase C view to land; this view just needs its manifest picked up
      by the existing `import.meta.glob`).
- [ ] **No changes** to `TaskSidebar.tsx` or `TaskDetail.tsx` from this
      PR — retiring `TaskSidebar` is Command Center consolidation
      (Phase D), a separate dependent PR.
- [ ] **No changes** to `InlineEventCard` — shared shell work (Non-goals
      §2).

## 12. Open questions

- **`c` shortcut semantics.** The task brief specifies `c` = "close
  task (modal)." No "close" concept exists in the current task
  lifecycle or in `TaskActions.tsx`; §6 maps it to the existing Delete
  flow. Shell spec §8.7 has the same undefined "close" binding for
  table rows, so the ambiguity is in the foundation spec, not
  introduced here. If "close" is meant to be a new non-destructive
  terminal state distinct from delete, that's backend surface to
  resolve at the shell-spec level first.
- **`useGates` enabling.** The hook doesn't take an `enabled` option
  today; §7.1's fix is a small local wrapper (or a shared addition to
  the hook). Left as an implementation-time call — doesn't change this
  view's external contract either way.
- **Extracting `TaskActions`'s button list.** The `.` "more actions"
  shortcut duplicates `TaskActions`'s internal visibility logic (§6).
  A follow-up `getVisibleTaskActions(task)` export would remove the
  duplication; not required for this view to ship.
- **Gate `task_ids` typing gap (§7.2).** The local cast works around a
  generated-client gap. The real fix — adding `task_ids` to the gate
  response Pydantic model — is a backend change outside this view's
  scope; flagged so it's tracked rather than silently worked around
  indefinitely.
