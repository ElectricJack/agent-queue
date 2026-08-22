# Pane View: `playbook-run-inspector` — Design

**Status:** design.
**Depends on:** `2026-08-22-dashboard-shell-v2-design.md` (shell primitives),
`2026-08-22-pane-plugin-interface-design.md` (contract this spec implements).
**Ship wave:** v2 (with `session-peek`, `console`). Ship priority within v2.

## 1. Goal

Give a user (or the supervisor, on their behalf) a live, actionable view of
one playbook run — node-by-node progress, per-node output, and any open
human-in-the-loop (HITL) pause — without leaving whatever they're doing.
Pane-hosted counterpart to `aq playbook inspect-run <run_id>` and the
existing `Runs` tab on `PlaybookDetail.tsx`
(`dashboard/src/pages/PlaybookDetail.tsx`), scoped to one run.

Use cases: click a run row (Command Center, `PlaybookDetail` Runs tab,
Activity Drawer) and get live state without navigating; supervisor pushes
the pane while discussing a paused run so the user can resolve it inline;
resume/cancel a run from wherever the user currently is.

## 2. Non-goals

- Not a playbook *definition* editor — that's `PlaybookDetail`'s Source tab.
- Not a run-list or multi-run comparison view — `list_playbook_runs` /
  `PlaybookDetail`'s Runs tab cover "which runs exist."
- Not the `gate.*` entity system (`gate_create`/`gate_resolve`, Activity
  Drawer's Gates tab). A playbook run's HITL pause is a different
  mechanism (`status="paused"` + `resume_playbook(run_id, human_input)`,
  no `gate_id`). "Open HITL gate" here is shorthand per the shell brief's
  wording — see §5.4 for detection, §13.5 for why the two systems differ.
- Not editing node trace or conversation history — read-only plus two
  run-level actions (resume, cancel).
- Not implementing the backend gaps this view surfaces (cancel command,
  richer per-node fields, node-level bus events) — scoped as follow-up in
  §13, with the view degrading gracefully against what exists today.

## 3. Manifest

```ts
// dashboard/src/panes/playbook-run-inspector/manifest.ts
import { z } from "zod";
import { PlayIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";

export const argsSchema = z.object({ runId: z.string().min(1) });
export type PlaybookRunInspectorArgs = z.infer<typeof argsSchema>;

export const manifest: PaneManifest<PlaybookRunInspectorArgs> = {
  id: "playbook-run-inspector",
  name: "Playbook Run",
  description: "Live node states, outputs, and HITL gates for one playbook run.",
  icon: PlayIcon,
  args_schema: argsSchema,
  // open_shortcut omitted per interface spec (no literal null; undefined = no shortcut)
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Inspect playbook run",
  palette_section: "Playbooks",
};
```

- `open_shortcut: null` — reached only by click-through or agent push,
  never a global hotkey; explicit `null` documents the decision.
- `route_scope: "cross-route"` (default) — user can navigate elsewhere
  while chatting with the supervisor about the open run.
- Icon (`PlayIcon`) is a placeholder — `PlaybookDetail.tsx` has no
  per-run icon convention today; check `pages/system/Playbooks.tsx` at
  implementation time before introducing a new one.

## 4. Args + validation

Single required arg, `runId: string`. No optional args — the view always
fetches its own data from `run_id`; `open()` and `setArgs()` funnel through
the same fetch path. `open()` fails loudly (console.error + no-op, per
plugin spec §6.1) on a missing/empty `runId` — that's a caller bug, not a
runtime state. An unknown-but-well-formed `runId` is a normal runtime case
(§9.2, "run not found"), not a schema violation. This view has no internal
navigation that would call `setArgs`; it's present on props per contract
but unused here.

## 5. Component

### 5.1 Layout

```
┌ [icon] Playbook Run ───────────────────────────── × ┐
│ [Refresh] [Resume] [Cancel] [Open playbook page]     │  toolbar (§6)
├───────────────────────────────────────────────────────┤
│ demo-review · v3 · run a1b2c3…  ⏱ 42s          [paused]│  run header strip
├───────────────────────────────────────────────────────┤
│ ▸ intake              completed   0.4s                │  node list (§5.2)
│ ▸ classify            completed   1.1s                │
│ ▾ human-review        paused      —          [running] │
├───────────────────────────────────────────────────────┤
│ node: human-review · status: paused · started 14:02:11 │  node detail (§5.3)
│ command / args_summary / output / error (§5.3.1)       │
│ ┌─ Waiting on you ───────────────────────────────────┐ │  HITL banner (§5.4)
│ │ [ Approve ] [ Reject ]  or reply: [______] [Send]   │ │
│ └─────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────┘
```

Pane body (header/toolbar frame is shell-provided per plugin contract §5)
has three stacked regions: run header strip; node list (top, ~40% of
remaining height, scrollable); node detail + HITL banner (bottom, ~60%,
scrollable).

### 5.2 Node list rows

Sourced from `InspectPlaybookRunResponse.node_trace` (server-computed
`duration_seconds` per entry — §7.1). Row: node id (mono), `StatusBadge`-
style pill for `entry.status` (`running|completed|failed|skipped`),
duration or `—`, and a pulsing dot when `entry.node_id === run.current_node`.

`node_trace` only contains nodes the run has actually visited — not-yet-
reached nodes aren't synthesized into the list (§13.7, gap vs. a true
graph view). Default selection on open: the last entry in `node_trace`,
already expanded (no separate collapsed/expanded row state exists — see
§6.3).

### 5.3 Node detail panel

Renders the selected entry: `node_id`, `status`, `started_at`/duration,
`transition_to` + `transition_method` (e.g. "→ finalize via llm") when
present, `tokens_used` if non-zero, and **`command`/`args_summary`/
`output`/`error`** per §5.3.1.

#### 5.3.1 command / args_summary / output / error — current data reality

`NodeTraceEntry` (`src/playbooks/runner.py`) today is only: `node_id`,
`started_at`, `completed_at`, `status`, `transition_to`,
`transition_method`, `tokens_used`. **No `command`, `args_summary`,
`output`, or per-node `error` field exists.** `conversation_history` (full
run-level LLM turns) and run-level `error` are the closest data, but
neither is indexed by node — correlating a turn to a node is an unreliable
heuristic (a node may make zero or several LLM calls).

This view renders these fields **optimistically against a proposed
backend extension** (§13.3: add the four fields to `NodeTraceEntry`,
thread through `node_trace`). Behavior:
- Present (post-extension): render normally — `output` in a scrollable
  `<pre>` (style matching `PlaybookDetail`'s `CompiledTab`), `error` in
  the existing red-alert treatment.
- Absent (today): render *"Node-level command/output detail isn't
  available for this run yet — see conversation history below,"* then the
  full (unscoped) `conversation_history` with a caption noting it's the
  whole run's, not just this node's.
- If the run failed (`run.error` set) and the selected node is the trace's
  last entry with `status === "failed"`, surface `run.error` in the
  node's error slot — reliable because the runner sets that status at the
  same point it records the run-level error (`runner.py` ~L1936-1938).

### 5.4 HITL gate banner

**Detection:** `run.status === "paused" && !run.waiting_for_event`.
`InspectPlaybookRunResponse` doesn't expose `waiting_for_event` yet (DB
column exists, response model doesn't — §13.4, cheap add). Without it, a
paused-for-external-event run would incorrectly show the gate banner;
once added, that case renders a plain info line ("Waiting for event:
`<type>`") instead.

When open, render the banner regardless of node selection (it's run
state, not node state): **Approve** → `resume_playbook({run_id,
human_input: "approve"})`; **Reject** → same with `"reject"`; free-text +
**Send** → same with the typed text (matches what `aq playbook resume`
already allows). Shared pending/error state; on success, invalidate
`["playbook-run", runId]` in addition to `useResumePlaybookRun`'s existing
`["playbook-runs"]` invalidation, so the pane updates without waiting for
the WS event.

## 6. Toolbar + shortcuts

### 6.1 Toolbar

```tsx
setToolbar([
  { id: "refresh", label: "Refresh", icon: ArrowPathIcon, onClick: refetch },
  ...(run?.status === "paused"
    ? [{ id: "resume", label: "Resume", icon: PlayIcon, onClick: openResumeFocus }]
    : []),
  { id: "cancel", label: "Cancel", icon: XCircleIcon, onClick: openCancelConfirm,
    disabled: isTerminal(run?.status) },
  { id: "open-playbook", label: "Open playbook page", icon: ArrowTopRightOnSquareIcon,
    onClick: () => navigate(`/playbooks/${encodeURIComponent(run.playbook_id)}`) },
]);
```

- `Resume` doesn't resume directly — it focuses the HITL banner's
  free-text field (§5.4). Sending an empty `human_input` would bypass the
  review the pane exists for; only rendered when `status === "paused"`.
- `Cancel` opens a confirm (model on `DeletePlaybookModal.tsx`) before
  calling the cancel mutation (§7.3, new backend work); disabled for
  every terminal status.
- `Open playbook page` navigates without closing the pane.

### 6.2 Shortcuts

```tsx
setShortcuts([
  { key: "ArrowUp", label: "Previous node", onFire: selectPrevNode },
  { key: "ArrowDown", label: "Next node", onFire: selectNextNode },
  { key: "Enter", label: "Expand node detail", onFire: expandSelectedNode },
  { key: "r", label: "Resume run", onFire: () => run?.status === "paused" && openResumeFocus() },
  { key: "x", label: "Cancel run", onFire: () => !isTerminal(run?.status) && openCancelConfirm() },
]);
```

### 6.3 `↑↓` vs `Enter`

`↑↓` already updates the detail panel on selection (§5.2) — there's no
separate expand toggle. `Enter` is registered per the requirement but is
a no-op beyond what `↑↓` did, except: if focus is on a toolbar button or
the HITL text field, `Enter` returns focus to the node list and
(re-)expands the current selection, matching the "expand" verb's intent
without inventing a second UI state.

## 7. Data + queries

### 7.1 REST: run detail — already exists

`inspect_playbook_run` is a registered command (`src/commands/
playbook_commands.py::_cmd_inspect_playbook_run`, category `"playbook"`)
with a typed response (`InspectPlaybookRunResponse`,
`src/api/models/playbook.py`). Codegen (`src/api/codegen.py`) turns this
into `POST /api/playbook/inspect-run`, body `{"run_id": "..."}`, already
present in the generated SDK (`packages/aq-ts-client/src/sdk.gen.ts`:
`inspectPlaybookRun`). **No new backend route needed for reads.** (This
corrects the brief's guess of a `/api/playbook-runs/{id}` path-param
route — that route doesn't exist; `src/api/routers/` only has
`proposals.py`. See §13.1.)

No hook exists yet in `dashboard/src/api/hooks.ts` — add:

```ts
export function useInspectPlaybookRun(runId: string | undefined) {
  return useQuery({
    queryKey: ["playbook-run", runId],
    queryFn: async () => {
      const { data } = await inspectPlaybookRun({ body: { run_id: runId! }, throwOnError: true });
      return data as InspectPlaybookRunResponse;
    },
    enabled: !!runId,
    // Fallback for node-level liveness the WS layer can't yet push (§7.4).
    refetchInterval: (query) => (query.state.data?.status === "running" ? 4_000 : false),
  });
}
```

`inspect_playbook_run` returns `{"error": "..."}` (not found) as a
codegen 422; with `throwOnError: true` this becomes a thrown error — treat
as "run not found" (§9.2), not a generic fetch failure.

### 7.2 REST: resume — already exists, already hooked

`useResumePlaybookRun()` already wraps `POST /api/playbook/resume` with
`{run_id, human_input}`. Reuse directly for Approve/Reject/free-text
(§5.4) — no new mutation hook. Note `human_input` is free text, not a
structured enum; "approve"/"reject" are a **client-side convention**
(literal strings), not backend-enforced (§13.5).

### 7.3 REST: cancel — does NOT exist (new backend work)

No `cancel_playbook_run` command exists, and `"cancelled"` isn't in the
`playbook_runs.status` CHECK constraint (`src/database/tables.py`:
`IN ('running','paused','completed','failed','timed_out')`).
`recover_workflow` operates on coordination workflows, not raw runs, and
doesn't cancel. Proposed (§13.2):
- Migration adding `'cancelled'` to the CHECK constraint (autogenerate
  reliably misses CHECK changes — review by hand).
- New command `cancel_playbook_run(run_id)`, category `"playbook"`:
  terminal run → `{"error": "run already <status>"}`; else set
  `status="cancelled"`, `completed_at=now()`, signal any in-process
  runner state to stop (needs runner-level investigation, out of scope
  here).
- Emits `notify.playbook_run_cancelled` (follows the existing
  `notify.playbook_run_*` family, `src/playbooks/runner_events.py`) — no
  `websocket.py` change needed, `"notify."` is already forwarded.
- New `CancelPlaybookRunResponse` model registered in
  `src/api/models/playbook.py::RESPONSE_MODELS` (dashboard `CLAUDE.md`
  convention), then regenerate the TS client and add
  `useCancelPlaybookRun()`.

Until this ships, `[Cancel]` is present (per requirement) but its
`onClick` surfaces "not available yet" (disabled + tooltip) rather than
silently no-op-ing or hitting a 404.

### 7.4 WS event filter

The brief names `playbook_run.node_started/node_completed/paused/resumed`.
Checked against reality:
- **`paused`/`resumed`** — real names are `notify.playbook_run_paused` /
  `notify.playbook_run_resumed` (`src/playbooks/runner_events.py`,
  forwarded because `websocket.py`'s `_FORWARDED_PREFIXES` includes
  `"notify."`), already typed in `dashboard/src/ws/types.ts`
  (`PlaybookRunPausedEvent`/`PlaybookRunResumedEvent`), each carrying
  `run_id`. Use these directly.
- **`node_started`/`node_completed`** — **do not exist as bus events.**
  The runner has an in-process `on_progress(event_name, node_id)`
  callback (`runner.py` ~L1592/1607/1651) firing these names, but it's
  never wired to `EventBus.publish` — nothing reaches `websocket.py` or
  the dashboard (§13.6).

**v1 behavior:** subscribe to the real `paused`/`resumed` events (plus
`completed`/`failed`/`timed_out` for terminal transitions, §8) and rely on
the 4s poll (§7.1) as the only source of node-level liveness until the bus
events land. When they do, swap the poll trigger for an event trigger the
same way `paused`/`resumed` already work.

## 8. Live-update model

```tsx
useEventStream({
  onEvent: (event) => {
    const runId = (event as { run_id?: string }).run_id;
    if (runId !== args.runId) return;
    if ([
      "notify.playbook_run_paused", "notify.playbook_run_resumed",
      "notify.playbook_run_completed", "notify.playbook_run_failed",
      "notify.playbook_run_timed_out",
      // future, once §7.4's gap closes:
      // "notify.playbook_run_node_started", "notify.playbook_run_node_completed",
    ].includes(event.event_type)) {
      queryClient.invalidateQueries({ queryKey: ["playbook-run", args.runId] });
    }
  },
});
```

Filtering is client-side on `event.run_id === args.runId` (no per-run WS
channel; same pattern as `useEventStream`'s existing `session.*`/`gate.*`
handling). Every matching event triggers invalidate-and-refetch of the
whole run rather than patching from the (partial) event payload — simpler
and correct even under dropped/out-of-order events. Terminal events also
stop the poll (via `refetchInterval`'s function form) and lock the view
into the read-only terminal presentation (§9.3).

## 9. Loading + error + terminal-state cases

**Loading:** run header strip + gray node-row placeholders while
`isLoading`; toolbar renders immediately, disabled (handlers reference
`run`, undefined during load).

**Run not found (§9.2):** thrown "not found" error → centered message
`Run <runId> not found.` `[Refresh]` stays enabled (retry); the other
toolbar actions disable — there's no `playbook_id` on an error response
to route `[Open playbook page]` to.

**Run already completed / terminal (§9.3):** status in
`{completed, failed, timed_out, cancelled}` — full node list + detail
still render, but: HITL banner never renders (`isHitlPaused` false by
definition); `[Resume]` doesn't render; `[Cancel]` is disabled; poll is
off. `StatusBadge`'s `statusColors` map is missing a `timed_out` entry
today — add it (§12). Read-only falls out of terminal status disabling
mutating affordances; no separate "read-only mode" flag needed.

**Other fetch errors (§9.4):** standard red-alert box (matches
`PlaybookDetail`'s `saveError` treatment) + `[Retry]` → `refetch()`.
Distinguished from "not found" by matching the error message text — the
dashboard has no structured error codes from codegen'd 422s today; noted
as a nice-to-have, not blocking.

## 10. Agent-push examples

```json
{
  "body_kind": "pane_open",
  "body": "Pulled up the review-gate run — it's paused waiting on you →",
  "pane_open": {
    "view": "playbook-run-inspector",
    "args": { "runId": "8f3e2a1c-9b4d-4e21-a7c5-1d2f3a4b5c6d" }
  }
}
```

```json
{
  "body_kind": "pane_open",
  "body": "Still running — 3 of 5 nodes done, no gate open yet →",
  "pane_open": {
    "view": "playbook-run-inspector",
    "args": { "runId": "8f3e2a1c-9b4d-4e21-a7c5-1d2f3a4b5c6d" }
  }
}
```

Emitted via:

```
aq message send --to user --to-id dashboard --thread dashboard:global \
    --body "Pulled up the review-gate run — it's paused waiting on you →" \
    --pane-open '{"view": "playbook-run-inspector", "args": {"runId": "8f3e2a1c-9b4d-4e21-a7c5-1d2f3a4b5c6d"}}'
```

Server-side validation checks `view` + `agent_pushable` against the pane
mirror registry (`src/panes/registry.py`, plugin spec §7) — this view's
entry must be `{"agent_pushable": True}` (§12).

## 11. Tests

**Manifest:** `id` matches directory; `argsSchema` accepts `{runId:"x"}`,
rejects `{}`/`{runId:""}`; `open_shortcut === null`;
`agent_pushable === true`; `palette_label`/`palette_section` match spec.

**Component:** node list renders from a mocked `useInspectPlaybookRun`;
selecting updates detail; default selection is the last trace entry;
`↑↓` clamps at boundaries; HITL banner renders iff
`status === "paused" && !waiting_for_event` (info line instead when
`waiting_for_event` set; absent for terminal statuses); Approve/Reject/
free-text each call the resume mutation with the right `human_input`;
`[Resume]` only when paused; `[Cancel]` disabled for every terminal
status; `[Open playbook page]` navigates to `/playbooks/:id`, encoded;
"run not found" rendering; terminal-state rendering (banner absent,
Resume absent, Cancel disabled, no second poll fetch via fake timers); a
matching-`run_id` WS event triggers invalidation, a non-matching one
doesn't. Close is shell-provided (`×`) — no view-owned close button to test.

**Registry:** covered by the shared
`dashboard/src/panes/__tests__/registry.test.ts` (plugin spec §9.2).

**Backend (new):** `tests/test_playbook_cancel_run.py` once §7.3 ships —
running→cancelled; terminal→error; emits `notify.playbook_run_cancelled`.
Extend `tests/test_pane_registry_parity.py` to cover this view's id.

## 12. Implementation checklist

- [ ] `dashboard/src/panes/playbook-run-inspector/manifest.ts` (§3).
- [ ] `.../index.tsx` — component (§5), toolbar (§6.1), shortcuts (§6.2).
- [ ] `dashboard/src/api/hooks.ts`: add `useInspectPlaybookRun` (§7.1).
- [ ] `.../hooks.ts` (optional): split out WS-subscription + poll logic
      (§8) if substantial.
- [ ] `StatusBadge.tsx`: add missing `timed_out` color entry (§9).
- [ ] `src/panes/registry.py`: add
      `"playbook-run-inspector": {"agent_pushable": True}`.
- [ ] Component + manifest tests (§11); run the parity test.
- [ ] **Backend follow-up** (tracked, not blocking the pane-shell PR, but
      blocking full parity with the ship requirement):
  - [ ] `cancel_playbook_run` command + `'cancelled'` status migration +
        `notify.playbook_run_cancelled` (§7.3).
  - [ ] `waiting_for_event` on `InspectPlaybookRunResponse` (§5.4).
  - [ ] `command`/`args_summary`/`output`/per-node `error` on
        `NodeTraceEntry` (§5.3.1).
  - [ ] `notify.playbook_run_node_started`/`_node_completed` bus events
        from the runner's existing `on_progress` callback (§7.4).
  - [ ] `useCancelPlaybookRun()` hook once the command exists; wire
        `[Cancel]` off its placeholder.
  - [ ] Regenerate the TS client after any response-model change.

## 13. Open questions

1. **Corrected, not actually open:** the brief's `/api/playbook-runs/{id}`
   guess doesn't exist. The real route is `POST /api/playbook/inspect-run`
   (§7.1) — flagging so implementation doesn't go looking for a route
   that was never built.
2. **Should `cancel_playbook_run` land in this view's PR or as a
   prerequisite?** Given "each pane view is a self-contained PR" (shell
   spec §10), leaning toward: ship this view with `[Cancel]` present-but-
   disabled (§7.3), land the backend command as a small separate PR that
   flips it live. Needs a decision before implementation starts.
3. **Per-node `command`/`args_summary`/`output`/`error` is the biggest
   gap.** Playbook nodes aren't uniformly "run a command, get output" —
   some are LLM-evaluated transitions, some `goto`, some wait on events.
   What to record per node *type* needs its own small design pass, not
   just four new dataclass fields.
4. **`waiting_for_event` is a cheap add** — the DB column exists; just
   needs surfacing on `InspectPlaybookRunResponse` and the command's
   result dict. Bundle with whichever PR next touches this response model.
5. **Approve/Reject as a `human_input` string convention** has no schema
   backing it — a playbook author could expect different words at a
   human-review node and this view's canned buttons would send the wrong
   ones. Whether the playbook markdown format should let a node declare
   its expected response vocabulary is a question for whoever owns
   playbook authoring (`docs/specs/design/playbooks.md`), deferred here.
6. **No node-level bus events today** (§7.4) — `on_progress` fires
   in-process only; finding the concrete call site to wire
   `EventBus.publish` from is implementation-level plumbing not traced in
   this pass. Until closed, "live" node updates come from the 4s poll,
   not a true push — satisfies the letter of the live-update requirement
   for `paused`/`resumed` but not its full spirit for node transitions.
7. **Node list only shows visited nodes**, not the full compiled graph —
   getting the full set would mean merging `playbook_graph_view`'s
   `nodes`/`edges` against `node_trace` by `node_id`. Worth a follow-up
   polish pass; skipped in v1 to keep this view to a single query.
