# Pane View — `proposal-preview` — Design

**Status:** design (approved by brainstorming pass 2026-08-22).
**Implements:** `2026-08-22-pane-plugin-interface-design.md` (the contract
every pane view follows).
**Depends on:** `2026-08-22-dashboard-shell-v2-design.md` (shell primitives:
`<ShellPane>`, `useShellPane`, palette, keyboard system).
**Backend:** Phase 6 spec-ingestion
(`docs/superpowers/plans/2026-08-21-dv2-phase6-spec-ingestion.md`) —
`task_proposals` table, `task_batch_{propose,commit,update,discard}`
commands, `GET /api/proposals/{proposal_id}`, `spec.approved` /
`proposal.ready` events, default-pipeline gate wiring. All already shipped
(commit `2e1a389c` and neighbors).

## 1. Goal

Give a human a way to review a Phase-6 spec-ingest proposal — the staged
task graph a `spec-ingest` agent produced via `task_batch_propose` — before
it becomes real tasks. The pane renders the proposal's header (id, source
spec, status, age), a compact node/edge graph of the proposed tasks, a
sortable list with per-task detail, and — when the proposal is `ready` —
`[Approve]` / `[Discard]` actions that resolve the associated HITL gate or
discard the batch outright.

This is the natural place to land when a `proposal.ready` gate is sitting
in the activity drawer waiting on a human: the drawer surfaces *that a
decision is needed*, this pane surfaces *what the decision is about*.

## 2. Non-goals

- Not an editor. The proposal's `tasks` / `edges` payload is read-only in
  this view. `task_batch_update` (draft/ready-only edit) is a real backend
  capability but wiring an editable graph UI to it is out of scope — v1 is
  preview + approve/discard only. Revisit if reviewers ask to tweak a
  proposal instead of discarding and re-running ingestion.
- Not a `proposal-list` view. There's no "browse all proposals" surface —
  proposals are reached via the `proposal.ready` gate (drawer → pane) or an
  agent push. A future `proposal-list` pane (all proposals across projects,
  filterable by status) is a plausible follow-up, not this spec.
- Not responsible for creating the gate or running `task_batch_commit`
  server-side — those are Phase 6 pipeline behaviors (default-pipeline's
  `proposal_ready_gate` and `commit_on_gate_resolve` nodes) that already
  exist. This view only calls the client-side actions that trigger them.
- Not building `GET /api/proposals` (list). Only the single-proposal read
  endpoint is needed here.

## 3. Manifest

```ts
// dashboard/src/panes/proposal-preview/manifest.ts
import { z } from "zod";
import { DocumentMagnifyingGlassIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";
import ProposalPreviewPane from "./index";

export const argsSchema = z.object({
  proposalId: z.string().min(1),
});

export type ProposalPreviewArgs = z.infer<typeof argsSchema>;

export const manifest: PaneManifest<ProposalPreviewArgs> = {
  id: "proposal-preview",
  name: "Proposal Preview",
  description: "Preview a staged task-batch proposal before approving it.",
  icon: DocumentMagnifyingGlassIcon,
  args_schema: argsSchema,
  // open_shortcut omitted per interface spec (no literal null; undefined = no shortcut)
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Preview proposal",
  palette_section: "Proposals",
};

export const Component = ProposalPreviewPane;
```

Directory shape (per plugin-interface spec §3):

```
dashboard/src/panes/proposal-preview/
├── manifest.ts
├── index.tsx
├── args.ts        # re-exports argsSchema from manifest.ts (kept trivial;
│                  # no separate file needed — see §4 note)
├── hooks.ts        # useProposal, useProposalGate
├── graph.ts         # dagre layout adapter for the small proposal graph
└── __tests__/
    ├── manifest.test.ts
    └── index.test.tsx
```

`args.ts` is skipped in the actual file tree — the schema is small enough
to live directly in `manifest.ts` per the plugin-interface spec's "optional
if it's large" guidance (§3). Listed above only to show the decision was
made, not deferred.

## 4. Args + validation

`{ proposalId: string }` — the `task_proposals.id` primary key
(`"prop-" + uuid4[:12]`, Task 1 of the Phase 6 plan). No other args.

Validation is entirely the shell's job per the plugin-interface contract
(§6.1): `open("proposal-preview", { proposalId })` runs
`manifest.args_schema.parse(args)` before the pane opens. A missing or
empty `proposalId` never reaches the component — `open` no-ops with a
`console.error`.

`setArgs` is exercised when the pane needs to pivot to a *different*
proposal without a full close/reopen — e.g. the toolbar's future "next
proposal" action (not built in v1, but the contract is free so we note it
in Open Questions). For v1, the component never calls `setArgs` itself.

## 5. Component

### 5.1 Data shape (from `GET /api/proposals/{proposalId}`, §7)

```ts
interface ProposalTask {
  tempId: string;
  title: string;
  description: string;
  priority?: number;
}

interface ProposalEdge {
  from: string;   // tempId or an existing task id
  to: string;     // tempId or an existing task id
  dep_type: "blocks" | "parent_child" | "waits_for" | "conditional_blocks" | "discovered_from";
}

interface ProposalDetail {
  proposal_id: string;
  project_id: string;
  source: string;       // e.g. "spec:projects/foo/specs/2026-08-21-thing.md"
  tasks: ProposalTask[];
  edges: ProposalEdge[];
  status: "draft" | "ready" | "committed" | "discarded";
}
```

The endpoint does not currently return `created_at` — see §9.4 (open
question) on how the header sources the proposal's age. The pane's header
requirement ("created_at") is satisfied via the associated gate's
`created_at` when a `ready` gate exists (§7.2), and omitted entirely for
`draft` / `committed` / `discarded` states where no open gate exists.

### 5.2 Layout

```
┌ [🔍] Proposal Preview ─────────────────────────────── [⟳] [📄] × ┐
│                                                                   │
│  prop-8f2a1c9d0e11                              [READY]          │
│  from spec: projects/foo/specs/2026-08-21-thing.md               │
│  proposed 4m ago                                                  │
│  ─────────────────────────────────────────────────────────────  │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              (compact dagre + xyflow graph)              │    │
│  │        ●──▶●──▶●                                         │    │
│  │             └──▶●                                        │    │
│  │  10–30 nodes, read-only, fit-to-view, no drag             │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                   │
│  Proposed tasks (4)                    sort: [title ▾] [prio ▾]  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │ setup-schema      Add task_proposals table…    P100  db  │    │
│  │ propose-commands  task_batch_propose/…         P100  db  │    │
│  │ proposal-api      GET /api/proposals/{id}…      P90  api │    │
│  │ spec-approve      spec_approve command…         P90  cmd │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                   │
│  ─────────────────────────────────────────────────────────────  │
│              [ Discard ]              [ Approve ]                │
│         (only rendered when status === "ready")                  │
└───────────────────────────────────────────────────────────────────┘
```

### 5.3 Header

- Proposal id (monospace, truncated with title tooltip for the full id).
- Status pill: `DRAFT` (gray) / `READY` (amber) / `COMMITTED` (green) /
  `DISCARDED` (red-gray, struck-through style).
- Source line: `from spec: <source>` with the `spec:` prefix stripped for
  display when present (`source` is free-form per the Phase 6 payload
  shape — Task 2's interface note: `"source": "spec:foo"` — so the pane
  strips a literal `spec:` prefix if found, otherwise shows `source`
  verbatim).
- Age: relative time from the associated gate's `created_at` when a `ready`
  gate is found (§7.2); hidden otherwise.

### 5.4 Graph

Reuses the Command Center Graph tab's rendering stack
(`dashboard/src/pages/command-center/{GraphCanvas,TaskNode,layout}.tsx`,
`@dagrejs/dagre` + `@xyflow/react`) rather than rebuilding it, per the
requirement to "reuse dagre + xyflow like Command Center's Graph tab, but
simpler." Concretely:

- `graph.ts` in this pane directory adapts `ProposalTask[]` /
  `ProposalEdge[]` into the same `{ nodes, edges }` shape
  `layoutGraph()` (command-center/layout.ts) already produces, but as a
  **local, proposal-scoped copy** — not a shared import — because the
  inputs differ structurally (`tempId` instead of a real task `id`; no
  `status`, `gates`, or `agents` fields to fold in; edges reference
  `from`/`to` instead of the graph API's `{from, to, dep_type}` — which
  happens to already match, so edge mapping is a passthrough).
- Layout: `dagre.graphlib.Graph({ rankdir: "TB", nodesep: 32, ranksep: 64 })`
  — tighter spacing than Command Center's (`nodesep: 40, ranksep: 100`)
  since proposal graphs are smaller (~10–30 nodes) and the pane is narrower
  than the full canvas.
- Node component: a trimmed variant of `TaskNode.tsx` — title + a small
  dep-count badge, no status icon (proposed tasks have no status yet), no
  agent avatar docking (`AgentAvatarLayer` is not reused — proposals have
  no agents). Node id = `tempId`.
  - Existing-task references (an edge endpoint that isn't a `tempId` in
    `tasks`) render as a dimmed "ghost" node showing only the truncated
    real task id, so the reviewer can see the proposal hooks into existing
    work without the pane needing a second fetch to resolve titles.
- `<ReactFlow>` config: `nodesDraggable={false}`, `nodesConnectable={false}`,
  `elementsSelectable={true}` (so clicking a node highlights the
  corresponding row in the task list below — no navigation, no pane
  mutation), `fitView` on mount and on args change, no minimap (graphs are
  small enough not to need one), `panOnScroll` + default zoom controls
  retained for graphs that do run toward 30 nodes.
- Container: fixed height (`~280px` at the default 480px pane width; grows
  with pane width per the resize contract in the shell spec, since wider
  panes fit visually larger graphs before scrolling is needed) with
  internal scroll/pan, never expands the pane's own layout.

### 5.5 Task list

Below the graph: a sortable table of `tasks`.

- Columns: title, description (truncated to ~80 chars, full text in a
  tooltip), priority (`priority ?? 100` — the Phase 6 `create_task` default
  used by `_create_one_task`, Task 3), and a derived "profile" chip. Note:
  the current `ProposalTask` payload shape (Task 2/3 interfaces) does not
  include a `profile` field — see §9.4 open question; the pane renders `—`
  for that column until the backend payload carries it, rather than
  inventing a value.
- Sort controls: `title` (default, alpha) / `priority` (desc). Client-side
  sort only — proposal task counts are small (~10–30), no server paging.
- Row click / `Enter` (when list has focus): highlights + centers the
  corresponding graph node. Does **not** open a nested pane (plugin
  interface spec §11 — nested panes are explicitly deferred; this view
  does not attempt it even for its own proposed tasks, since they don't
  have real task ids to open a `task-detail` pane against until after
  commit).

### 5.6 Actions (only when `status === "ready"`)

```tsx
<div className="flex justify-between gap-3 border-t border-white/10 pt-3">
  <button onClick={handleDiscard} className="btn-danger-ghost">
    Discard
  </button>
  <button onClick={handleApprove} className="btn-primary">
    Approve
  </button>
</div>
```

- Hidden entirely for `draft`, `committed`, `discarded` (§9 covers what
  renders instead).
- Both buttons disabled while their respective mutation is in flight;
  button label swaps to "Approving…" / "Discarding…".
- Both actions close the pane on success (the proposal's terminal state is
  reached; nothing left to review) — `close()` is called after the mutation
  resolves and the relevant queries are invalidated (§7.4). Actions do not
  close the pane on failure — the error surfaces inline above the buttons
  (§9.3) so the user can retry without re-opening.

## 6. Toolbar + shortcuts

### 6.1 Toolbar (`setToolbar`)

```ts
setToolbar([
  {
    id: "refresh",
    label: "Refresh",
    icon: ArrowPathIcon,
    onClick: () => queryClient.invalidateQueries({ queryKey: ["proposal", args.proposalId] }),
  },
  {
    id: "view-source",
    label: "View spec source",
    icon: DocumentTextIcon,
    disabled: !sourceSpecPath,
    onClick: () => open("spec-doc-reader", { path: sourceSpecPath! }),
  },
]);
```

- `sourceSpecPath` is derived from `data.source`: strip a leading `spec:`
  prefix if present, else treat the whole string as the path. If `source`
  doesn't look like a spec path at all (empty, or doesn't end in `.md`),
  "View spec source" renders disabled rather than opening a broken
  `spec-doc-reader` pane.
- Both actions carry `data-hotkey` per the plugin-interface spec §5.1 so
  the cheat sheet enumerates them under this pane's section.

### 6.2 Shortcuts (`setShortcuts`)

```ts
setShortcuts([
  { key: "r", label: "Refresh", onFire: handleRefresh },
  { key: "s", label: "View spec source", onFire: handleViewSource },
  ...(status === "ready"
    ? [
        { key: "a", label: "Approve", onFire: handleApprove },
        { key: "d", label: "Discard (confirm)", onFire: handleDiscardWithConfirm },
      ]
    : []),
]);
```

- `a` / `d` are only ever registered while `status === "ready"` — they
  don't exist as dead bindings in other states, matching the shell's
  per-entity shortcut model (shell spec §8.7) where bindings are only
  live when the relevant entity/state is in focus.
- `d` opens a lightweight inline confirm (not a modal — a second click
  target replaces the Discard button briefly: "Really discard? [Yes] [No]",
  auto-reverts after 4s or on `Esc`) rather than a full modal, since the
  action is reversible in effect (nothing has been committed yet) but
  irreversible in state (a discarded proposal can't be un-discarded — the
  spec-ingest agent would have to re-propose). This mirrors the shell's
  general "with confirm" pattern for destructive one-key actions (shell
  spec §8.7's `k` kill-session, `p` pause-project).
- `r` and `s` re-use the same handlers as the toolbar buttons (`refresh`,
  `view-source`) — no duplicated logic.
- Shortcuts only fire when the pane holds focus (plugin-interface §5.2).

## 7. Data + queries

### 7.1 `useProposal(proposalId)` — `dashboard/src/panes/proposal-preview/hooks.ts`

```ts
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { legacyFetch } from "../../api/legacy-fetch";

export interface ProposalDetail {
  proposal_id: string;
  project_id: string;
  source: string;
  tasks: Array<{ tempId: string; title: string; description: string; priority?: number }>;
  edges: Array<{ from: string; to: string; dep_type: string }>;
  status: "draft" | "ready" | "committed" | "discarded";
}

export function useProposal(proposalId: string) {
  return useQuery<ProposalDetail>({
    queryKey: ["proposal", proposalId],
    enabled: !!proposalId,
    queryFn: async () => {
      const r = await legacyFetch(`/api/proposals/${proposalId}`);
      if (r.status === 404) throw new Error("proposal not found");
      if (!r.ok) throw new Error(`proposal fetch ${r.status}`);
      return (await r.json()) as ProposalDetail;
    },
    refetchInterval: (query) =>
      query.state.data?.status === "ready" ? 15_000 : false,
  });
}
```

Uses `legacyFetch`, not the generated `@aq/ts-client`, following the same
pattern `GhostOverlay.tsx` (`dashboard/src/pages/command-center/`) already
established for this exact endpoint. **Deviation from the dashboard's
CLAUDE.md "never call fetch directly — use the generated SDK" rule is
intentional and pre-existing**: `GET /api/proposals/{id}` is a plain
FastAPI route (`src/api/routers/proposals.py`) that was never registered
with a Pydantic model in `src/api/models/*.py`'s `RESPONSE_MODELS`, so it
isn't in the generated client. Per the CLAUDE.md's own escape hatch
("`legacy-fetch.ts` exists only for routes that aren't in the generated
SDK"), this qualifies. §12 checklist item flags registering a
`ProposalResponse` model as the proper fix — once done, `useProposal`
swaps to the generated `getProposal` call and `queryFn` shrinks
accordingly; this spec does not block on that migration to ship.

`refetchInterval` polls only while `ready` (waiting on a human decision
that might resolve out-of-band, e.g. someone approves the gate from the
activity drawer instead of this pane) — `draft`/`committed`/`discarded`
are terminal or externally-driven-only and don't need polling (live-update
covers `committed`/`discarded` transitions via WS, §8).

### 7.2 `useProposalGate(projectId, proposalId)` — finding the gate to approve

There is no `GET /api/proposals/{id}/gate` endpoint. The default-pipeline
(Phase 6 plan, Task 6) creates the gate with
`await_id: "proposal:{{event.proposal_id}}"`, so the pane locates it by
filtering the existing gate-list query:

```ts
import { useGates } from "../../api/hooks";

export function useProposalGate(projectId: string, proposalId: string) {
  const gatesQuery = useGates({ projectId, status: "open" });
  const gate = gatesQuery.data?.find(
    (g) => g.await_id === `proposal:${proposalId}`,
  );
  return { ...gatesQuery, gate };
}
```

`useGates` (`dashboard/src/api/hooks.ts`) already exists and returns
`GateSummary[]` (via the generated `gateList` call), and `GateSummary`
(`src/api/models/gate.py`) already carries `await_id` and `created_at` —
no backend change needed for this lookup. This is also the source for the
header's "proposed Nm ago" (§5.3): `gate.created_at`.

### 7.3 Approve — resolves the gate, not a direct commit call

```ts
import { useResolveGate } from "../../api/hooks";

const resolveGate = useResolveGate();

async function handleApprove() {
  if (!gate) return; // no open gate found — see §9.3
  await resolveGate.mutateAsync({
    gate_id: gate.id,
    resolved_by: "dashboard", // matches the convention used elsewhere in the
                               // dashboard for human-initiated gate resolution
    resolution: "approved",
  });
  close();
}
```

`useResolveGate` (`dashboard/src/api/hooks.ts`) already exists and calls
the generated `gateResolve` SDK function. Approving here does **not** call
`task_batch_commit` directly — resolving the gate with `resolution:
"approved"` is what the default-pipeline's `commit_on_gate_resolve` node
(Phase 6 Task 6) reacts to, and that node is what calls
`task_batch_commit`. This keeps the pane a thin client over the existing
gate-resolution path rather than a second commit trigger, and matches the
requirement text ("Approve action → calls the daemon's approve path for
the associated HITL gate").

### 7.4 Discard — direct command call

```ts
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { taskBatchDiscard } from "@aq/ts-client"; // once the client is regenerated
                                                    // to include Phase 6 endpoints —
                                                    // see §12 checklist

const queryClient = useQueryClient();
const discard = useMutation({
  mutationFn: async () => {
    // Same legacyFetch fallback as useProposal (§7.1) until task_batch_discard
    // is registered with a response model and the client regenerated.
    const r = await legacyFetch(`/api/commands/execute`, {
      method: "POST",
      body: JSON.stringify({ command: "task_batch_discard", args: { proposal_id: args.proposalId } }),
    });
    if (!r.ok) throw new Error(`discard failed: ${r.status}`);
    return r.json();
  },
  onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: ["proposal", args.proposalId] });
    close();
  },
});
```

Calls `task_batch_discard` directly, per the requirement. There is no
dedicated `POST /api/proposals/{id}/discard` REST route today — only the
generic command-execute path (`execute_router`, mounted in
`src/api/app.py`) reaches arbitrary `CommandHandler` commands. §13 flags
whether a dedicated REST verb or a generated-client entry is worth adding
so this isn't hand-rolled JSON — not required to ship v1.

## 8. Live-update model

### 8.1 Existing events this pane already benefits from

- `gate.resolved` (already a first-class WS event —
  `dashboard/src/ws/types.ts` `GateResolvedEvent`, and already wired into
  `useEventStream`'s prefix-based invalidation: it invalidates
  `["gates"]`, `["gate"]`, `["tasks"]`, `["explain"]` on every
  `gate.created` / `gate.resolved` / `gate.expired` frame). Because
  `useProposalGate` (§7.2) is built on `useGates`, a `gate.resolved` event
  — whether it happened from this pane, the activity drawer, or another
  browser tab — already invalidates the gate lookup for free. No new
  client wiring needed for this half of the live-update story.

### 8.2 Gap: no proposal-status WS event exists

The Phase 6 event schema (`src/event_schemas.py`) only adds
`spec.approved` and `proposal.ready` — both fire once, at proposal
creation. Nothing fires when a proposal transitions `ready → committed`
(via `task_batch_commit`, invoked either through this pane's approve flow
indirectly via the gate, or via `task_batch_discard`'s
`ready|draft → discarded`). Today the pane's freshness for those
transitions depends on:

- Its own mutations (`handleApprove` via gate resolve, `handleDiscard`
  directly) invalidating `["proposal", proposalId]` — covers the case
  where *this pane* is the one that acted.
- The 15s poll while `status === "ready"` (§7.1) — covers the case where
  someone else acted (approved from the drawer, or another tab/agent
  discarded it) but bounds staleness to ~15s.

**Proposed new event** (this spec proposes it; not yet in
`src/event_schemas.py` or `dashboard/src/ws/types.ts` — a backend
follow-up, tracked in §13):

```python
"proposal.status_changed": {
    "required": ["project_id", "proposal_id", "status"],
    "optional": [],
},
```

Emitted from `task_batch_commit` (on success, `status="committed"`) and
`task_batch_discard` (on success, `status="discarded"`) in
`src/commands/proposal_commands.py`, alongside the existing
`proposal_queries.update_proposal(..., status=...)` calls in those two
methods (`TaskProposalCommandsMixin._cmd_task_batch_commit` /
`_cmd_task_batch_discard`). Frontend addition:

```ts
// dashboard/src/ws/types.ts
export interface ProposalStatusChangedEvent extends BaseEvent {
  event_type: "proposal.status_changed";
  project_id: string;
  proposal_id: string;
  status: "committed" | "discarded";
}
```

and a case in `useEventStream`'s prefix block:

```ts
if (type === "proposal.status_changed") {
  const pid = (event as ProposalStatusChangedEvent).proposal_id;
  queryClient.invalidateQueries({ queryKey: ["proposal", pid] });
  return;
}
```

This closes the gap immediately instead of relying on the 15s poll, and
gives a future `proposal-list` pane (§2 non-goals) something to subscribe
to as well. Until this event ships, the pane functions correctly but with
up-to-15s staleness for externally-driven commit/discard — documented as
a known gap, not a blocker (§13).

### 8.3 What the pane subscribes to at the component level

The pane does not call `useEventStream` directly — event handling is
centralized in the WS provider (`dashboard/src/ws/EventStreamProvider.tsx`)
which every page already mounts once, and query invalidation is how
components "subscribe" (React Query re-renders on invalidated-then-refetched
data). The pane only needs `useProposal` and `useProposalGate`'s normal
React Query subscriptions — no bespoke WS listener in this pane's own code.

## 9. Loading, error, and terminal-state cases

### 9.1 Loading

`useProposal`'s `isPending` — header shows a skeleton pill row, graph area
shows a centered spinner sized to the same fixed height as the real graph
(§5.4) so the pane doesn't jump on load, task list shows 3 skeleton rows.

### 9.2 Not found / fetch error

`useProposal`'s `isError` (404 from `GET /api/proposals/{id}`, or any
non-2xx) — the pane renders only:

```
[icon] Proposal not found (or failed to load)
prop-8f2a1c9d0e11
[Retry]  [Close]
```

`[Retry]` re-triggers the query (`refetch()`); `[Close]` calls `close()`.
No graph, no task list, no toolbar actions beyond what's already
registered (Refresh still works and is the same as Retry here).

### 9.3 `ready` but no matching gate found

Can happen transiently (gate creation lags proposal-ready by one pipeline
tick) or if the pipeline's gate-creation step failed. `handleApprove` is a
no-op guarded by `if (!gate) return` (§7.3); the Approve button instead
renders disabled with a small inline note: "Waiting for approval gate to
appear…" and the pane's `useProposalGate` continues polling via
`useGates`' own `refetchInterval` (20s, per the existing hook in
`dashboard/src/api/hooks.ts`). Discard remains available regardless — it
doesn't depend on the gate.

### 9.4 Mutation failure (approve / discard)

Inline error banner above the action row: `Approve failed: <message>` /
`Discard failed: <message>`, dismissible, does not close the pane (§5.6).
Both actions remain re-clickable after a failure.

### 9.5 Terminal states — `committed` / `discarded`

No action row (§5.6). Instead, a status banner replaces it:

- `committed`: "Committed — N tasks created." with N derived from
  `data.tasks.length` (the payload's task count; the endpoint doesn't
  currently echo back the real task ids created by commit — see §9.4's
  earlier note on payload gaps, and §13). No link to the created tasks in
  v1 since the pane has no real task ids to link to; a `task-detail` deep
  link is a natural follow-up once `task_batch_commit`'s result (already
  returning `task_ids` per the Task 3 interface) is surfaced through the
  read endpoint too.
- `discarded`: "Discarded — no tasks were created."

Both states still render the graph + task list (useful for post-hoc
review — "what did the agent propose that we rejected/accepted") — only
the action row and the polling behavior (§7.1) differ.

### 9.6 `draft` state

Per the Phase 6 payload shape, `task_batch_propose` sets status straight to
`ready` on success (Task 3: `_cmd_task_batch_propose` calls
`update_proposal(..., status="ready")` immediately after insert) — `draft`
is reachable only via `task_batch_update` being called on a `ready`
proposal without immediately re-marking it ready, which the shipped
`_cmd_task_batch_update` does not do (it re-sets `status="ready"` at the
end of every update). Practically, **`draft` is unreachable through any
shipped code path today.** The pane still handles it defensively (same
rendering as `ready` minus the action row — matches §5.6's "hidden
entirely for draft") rather than treating it as an error, since the schema
allows it and a future editing UI (§2 non-goals) would produce real
`draft` proposals.

## 10. Agent-push examples

Per the plugin-interface spec §6.5, the spec-ingest agent (or the
supervisor, relaying on its behalf) pushes this view once a proposal is
ready, via the CLI helper:

```bash
aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "Proposed 4 tasks from projects/foo/specs/2026-08-21-thing.md — ready for review →" \
    --pane-open '{"view": "proposal-preview", "args": {"proposalId": "prop-8f2a1c9d0e11"}}'
```

Per-project supervisor variant (thread scoped to that project's chat
instead of the global one):

```bash
aq message send --to user --to-id dashboard --thread dashboard:demo \
    --body "Spec ingest finished — 12 tasks proposed, take a look →" \
    --pane-open '{"view": "proposal-preview", "args": {"proposalId": "prop-1a2b3c4d5e6f"}}'
```

Because `manifest.agent_pushable` is `true`, the server-side mirror in
`src/panes/registry.py` (plugin-interface spec §7) must carry a matching
entry:

```python
# src/panes/registry.py
SERVER_PANE_REGISTRY = {
    # ...existing entries...
    "proposal-preview": {"agent_pushable": True},
}
```

Client behavior on arrival (plugin-interface spec §6.5, unchanged by this
view — no special-casing needed): the chat transcript renders an
`InlineEventCard` `pane_open` chip ("opened proposal preview →"), and
`useChatTranscript` dispatches `pane.open("proposal-preview", {
proposalId })`, which triggers the same `argsSchema` validation as any
user-initiated open (§4).

## 11. Tests

`dashboard/src/panes/proposal-preview/__tests__/`

**`manifest.test.ts`** (per plugin-interface spec §9.1):
- `manifest.id === "proposal-preview"` matches the directory name.
- `argsSchema` accepts `{ proposalId: "prop-abc" }`, rejects `{}` and
  `{ proposalId: "" }`.
- `manifest.open_shortcut` is `null` (not merely absent — the requirement
  is explicit).
- `manifest.agent_pushable === true`.
- `manifest.palette_label === "Preview proposal"`,
  `manifest.palette_section === "Proposals"`.

**`index.test.tsx`** (component; mocks `useProposal` / `useProposalGate` /
`useResolveGate` at the module level, per existing dashboard test
conventions for hook-heavy components):
- Renders header (id, status pill, source line) for a `ready` proposal
  fixture.
- Renders the graph container with the right node count (asserts on
  `data-testid="proposal-graph"` child count, not pixel layout).
- Task list renders one row per proposed task; sort toggle re-orders rows.
- `ready` state shows both action buttons; `draft` / `committed` /
  `discarded` fixtures hide them and show the matching terminal banner
  (§9.5/§9.6).
- Clicking Approve with a resolved `gate` fixture calls
  `resolveGate.mutateAsync` with `{ gate_id, resolved_by: "dashboard",
  resolution: "approved" }` and then `close()`.
- Clicking Approve with `gate: undefined` is a no-op (button disabled,
  handler guarded) — §9.3.
- Clicking Discard → confirm → calls the discard mutation with
  `proposal_id` and then `close()` on success.
- A failed approve/discard mutation renders the inline error banner and
  does **not** call `close()`.
- 404 fixture renders the not-found state (§9.2) with working `Retry` /
  `Close`.
- `setToolbar` is called with exactly two actions (`refresh`,
  `view-source`); `view-source` is `disabled: true` when `source` doesn't
  parse to a spec path.
- `setShortcuts` includes `a`/`d` only when `status === "ready"`; excludes
  them otherwise.
- Keyboard: simulating `r` fires the same handler as the toolbar Refresh
  button (asserts the invalidate call, not a UI diff).

## 12. Implementation checklist

Per the plugin-interface spec's "building a new pane view" checklist
(§10), specialized for this view:

- [ ] `dashboard/src/panes/proposal-preview/manifest.ts` (§3).
- [ ] `dashboard/src/panes/proposal-preview/hooks.ts` — `useProposal`,
      `useProposalGate` (§7.1, §7.2).
- [ ] `dashboard/src/panes/proposal-preview/graph.ts` — local dagre
      adapter for `ProposalTask[]`/`ProposalEdge[]` (§5.4).
- [ ] `dashboard/src/panes/proposal-preview/index.tsx` — component
      implementing the `PaneViewProps` contract (§5, §6).
- [ ] Add `"proposal-preview": {"agent_pushable": True}` to
      `src/panes/registry.py` (§10) and confirm the parity test
      (`tests/test_pane_registry_parity.py`, plugin-interface spec §7)
      passes.
- [ ] `dashboard/src/panes/proposal-preview/__tests__/manifest.test.ts`
      and `index.test.tsx` (§11).
- [ ] Run the pane registry test
      (`dashboard/src/panes/__tests__/registry.test.ts`) — confirms no
      `open_shortcut` collision (moot here since it's `null`) and that
      the id resolves.
- [ ] Register a `ProposalResponse` Pydantic model in
      `src/api/models/proposals.py` (new file — doesn't exist yet; the
      router currently returns a bare `dict`) so the generated
      `@aq/ts-client` picks up `GET /api/proposals/{id}`, then swap
      `useProposal` off `legacyFetch` (§7.1's flagged deviation). Not
      blocking for v1 ship but should land before/alongside this pane so
      the CLAUDE.md convention isn't violated indefinitely.
- [ ] Backend: add `proposal.status_changed` to `src/event_schemas.py`
      and emit it from `_cmd_task_batch_commit` /
      `_cmd_task_batch_discard` in `src/commands/proposal_commands.py`
      (§8.2). Frontend: add `ProposalStatusChangedEvent` to
      `dashboard/src/ws/types.ts` and the invalidation case in
      `dashboard/src/ws/useEventStream.ts`.
- [ ] Manual verification: push a real proposal via
      `aq surface exec task_batch_propose ...`, confirm the pipeline gate
      appears in the drawer, open this pane via the drawer's gate row
      `Enter`-to-open-associated-pane behavior (shell spec §6.3 — task
      gates open `task-detail`; for a proposal gate this pane needs the
      drawer's `Enter` handler taught to route `await_id.startsWith("proposal:")`
      to `proposal-preview` with `{ proposalId: await_id.slice("proposal:".length) }`
      instead of `task-detail` — flag this as a small addition to the
      activity-drawer's gate-row `Enter` handler, tracked in §13, since
      the drawer spec predates this pane and its "open associated task"
      wiring assumed every gate maps to a task).
- [ ] Approve end-to-end: confirm `gate.resolved` → pipeline's
      `commit_on_gate_resolve` node → `task_batch_commit` → tasks appear
      in `aq task list`.
- [ ] Discard end-to-end: confirm `task_batch_discard` flips status and
      the pane reflects `discarded` without a page reload.

## 13. Open questions

- **`GET /api/proposals/{id}` doesn't return `created_at`.** The pane
  works around this by borrowing the associated gate's `created_at`
  (§5.3, §7.2), which is `None`/absent once the gate resolves — so the
  age line silently disappears the moment a proposal leaves `ready`. A
  cleaner fix is adding `created_at`/`updated_at` to the endpoint's
  response (the underlying `task_proposals` row already has both columns,
  Task 1) — small backend change, not required to ship this pane.
- **No `profile` field on proposed tasks.** The task-list column exists
  per the requirements but the payload shape from Task 2/3 of the Phase 6
  plan has no `profile_id` on proposed tasks (spec-ingest agents don't
  choose a profile — `_create_one_task` doesn't accept one either).
  Rendered as `—` for now (§5.5). If profile selection becomes part of
  proposals later, this column activates without a pane-side schema
  change (it's already present in the layout).
- **No dedicated REST verb for `task_batch_discard`.** §7.4 routes through
  the generic command-execute path. Worth a follow-up
  `POST /api/proposals/{id}/discard` (and `/commit` for symmetry, though
  commit is pipeline-driven and arguably shouldn't have a direct REST
  door) once more panes need direct command calls and the pattern
  repeats.
- **Activity drawer's gate → pane routing assumes task gates.** Flagged in
  §12's checklist — the drawer's `Enter`-on-gate-row behavior (shell spec
  §6.3) needs a proposal-aware branch. Small, but it's drawer-spec
  territory, not this pane's; noting it here since it's a real integration
  gap discovered while writing this spec, not a hypothetical.
- **`proposal.status_changed` (§8.2) is a new event this spec proposes.**
  Not yet implemented backend-side. Ship order: this pane works without
  it (15s poll covers the gap while `ready`), but the event should land
  in the same PR family if reasonably scoped, since the gap it closes
  (external commit/discard staleness) is exactly the kind of surprise a
  reviewer would file as a bug otherwise.
- **Nested "jump to created tasks" after commit.** §9.5 notes
  `task_batch_commit`'s response already carries `task_ids` (Task 3
  interface) but the read endpoint doesn't echo them back post-commit, so
  this pane can't deep-link into the real tasks it created. Surfacing
  `task_ids` on the `GET /api/proposals/{id}` response for `committed`
  proposals (reading them back from... there's no `task_proposals` column
  for this today; would need either a new column or a join against
  `tasks.metadata.proposal_source`) is real product value but a distinct
  chunk of backend work, deferred.
