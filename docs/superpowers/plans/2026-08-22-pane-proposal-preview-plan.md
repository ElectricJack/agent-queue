# Pane View — `proposal-preview` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `proposal-preview` dashboard pane view — the surface a
human uses to review a Phase-6 spec-ingest task-batch proposal (header,
compact graph, sortable task list, approve/discard) — plus the small
backend event and server-side registry pieces it depends on.

**Architecture:** A self-contained pane view under
`dashboard/src/panes/proposal-preview/` implementing the pane-plugin
contract (manifest + component + hooks + local graph adapter). Because
the shared pane-plugin infrastructure described in the companion specs
(`dashboard/src/panes/types.ts`, `registry.ts`, `useShellPane`) does not
exist in this codebase yet — confirmed by direct inspection, see
Deviations below — this plan builds the minimal real (non-stub) slice of
that infrastructure first, then the view on top of it. On the backend, a
new `proposal.status_changed` bus event closes a live-update gap the
pane's design doc flags, and a static Python dict mirrors the frontend
pane registry for `--pane-open` validation.

**Tech Stack:** React 19, TanStack Query v5, `@dagrejs/dagre` +
`@xyflow/react` (already dependencies), Vitest + React Testing Library
(net-new — bootstrapped in Task 6), `zod` (net-new dependency), Python
3.12 / pytest / SQLAlchemy Core (existing backend stack).

**Spec:**
- `docs/superpowers/specs/2026-08-22-pane-proposal-preview-design.md` (primary)
- `docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md` (shared pane contract)
- `docs/superpowers/specs/2026-08-22-dashboard-shell-v2-design.md` (shell context)

## Global Constraints

- Icons: `@heroicons/react/24/outline` only. Never introduce `LucideIcon`
  or any other icon library (plugin-interface spec §4 bans it; its own
  §5 `PaneToolbarAction.icon: LucideIcon` is a spec typo — this plan uses
  the heroicon type everywhere, see Deviations).
- `legacyFetch` (not raw `fetch`) for the two routes that aren't in the
  generated `@aq/ts-client` SDK (`GET /api/proposals/{id}`,
  `POST /api/commands/execute` for `task_batch_discard`) — this is the
  documented escape hatch in `dashboard/CLAUDE.md`; every other daemon
  call in this plan goes through the existing generated-SDK hooks
  (`useGates`, `useResolveGate`).
- React Query key convention: `["proposal", proposalId]` for the single
  proposal detail (matches the pre-existing `GhostOverlay.tsx` usage of
  the same endpoint), `["gates", ...]` untouched (owned by `useGates`).
- `manifest.open_shortcut` is `undefined` (omitted), never a literal
  `null` — plugin-interface spec §4 is explicit that a `null` literal
  violates the `string?` type. See Deviations for how this reconciles
  with the per-pane spec's own (contradictory) test requirement.
- Directory shape for the view: `dashboard/src/panes/proposal-preview/{manifest.ts,index.tsx,hooks.ts,graph.ts,nodes.tsx,__tests__/}` per plugin-interface spec §3.
- Every new/modified TypeScript file must satisfy the existing
  `tsconfig.app.json` strict flags (`strict`, `noUnusedLocals`,
  `noUnusedParameters`, `noFallthroughCasesInSwitch`,
  `noUncheckedIndexedAccess`) — this repo already runs `tsc -b` in CI via
  `npm run build`.

## Deviations from the input specs (established during research, not to be re-litigated per-task)

1. **Pane-plugin infrastructure does not exist yet.** `dashboard/src/panes/`,
   `dashboard/src/shell/`, `src/panes/registry.py`, and any
   `ActivityDrawer` component are all absent from the codebase (verified
   by direct `ls`/`grep`). The companion specs assume this infra ships in
   a separate "Phase B — shell foundation" PR family. This plan does not
   build the full shell (no `ShellPane` host component, no Zustand/React
   Query palette, no drawer UI) — that is out of scope for a single pane
   view. It **does** build the minimal real pieces this pane needs to
   function and be testable on its own: `dashboard/src/panes/types.ts`
   (shared manifest/component prop types — pure interfaces, no
   behavior), `dashboard/src/panes/registry.ts` (the
   `import.meta.glob`-based aggregator the plugin-interface spec
   describes verbatim in §4.1), and `dashboard/src/shell/useShellPane.ts`
   (a first-cut, non-fake implementation of shell spec §5.1's pane-state
   contract — `open`/`close`/`state`, nothing more). These are written to
   the *finalized* contract in the specs, so a future shell-foundation PR
   is expected to extend this code in place, not replace it.
2. **`open_shortcut: null` vs. omitted.** The plugin-interface spec (§4)
   forbids a literal `null` for `open_shortcut`; the per-pane design spec
   (§3, §11) writes `open_shortcut: null` and even asserts it in its test
   list. Since the plugin-interface spec is the authoritative shared
   contract ("the only thing every per-pane spec depends on"), this plan
   omits the field entirely and asserts `manifest.open_shortcut` is
   `undefined`, not `null`.
3. **`PaneToolbarAction.icon` type.** Plugin-interface spec §4 bans
   `LucideIcon`; its own §5 snippet types `PaneToolbarAction.icon` as
   `LucideIcon`. This plan uses the same heroicon-based `HeroIcon` alias
   for both `PaneManifest.icon` and `PaneToolbarAction.icon`.
4. **Event name is `proposal.status_changed`, not
   `notify.proposal_status_changed`.** The per-pane design spec §8.2
   defines the bus event as `"proposal.status_changed"`, registered in
   `event_schemas.py`'s `_SPEC_SCHEMAS` block (same family as the
   already-shipped `spec.approved` / `proposal.ready`) and emitted via
   the existing `_emit_proposal_event` helper. The `notify.*` prefix is
   reserved for a structurally different event family
   (`_NOTIFY_SCHEMAS`, all carrying a full `Task`/`Agent` payload) that
   this event does not belong to. This plan follows the design spec's
   precise implementation, not the shorter task-list shorthand.
5. **Activity drawer's gate-row dispatch.** Since `ActivityDrawer` does
   not exist in code, this plan cannot "update the shell's dispatch
   table." Instead it ships the dispatch logic as a standalone, pure,
   fully-tested function (`dashboard/src/shell/paneGateDispatch.ts`,
   Task 17) implementing shell spec §6.3's gate-type switch exactly.
   When `ActivityDrawer` is built, its Enter-key handler calls this
   function directly. This is flagged again in Task 17 and in the
   plan's manual-verification checklist.
6. **Frontend test tooling does not exist.** No `vitest`/`jest` config,
   no test script, zero `*.test.*` files anywhere under `dashboard/`.
   Task 6 bootstraps Vitest + React Testing Library — a real prerequisite
   for every test step the per-pane spec requires, not optional
   yak-shaving.
7. **`ProposalResponse` Pydantic model / SDK regeneration** (per-pane
   spec §12's checklist item) is explicitly flagged there as "not
   blocking for v1 ship" and is not in the user's 14-item task list.
   Not built in this plan; `legacyFetch` stays the client for
   `GET /api/proposals/{id}` as the spec's own escape-hatch note
   describes.

---

### Task 1: Register `proposal.status_changed` event schema

**Files:**
- Modify: `src/event_schemas.py:747-756` (`_SPEC_SCHEMAS` dict)
- Test: `tests/test_event_schemas.py`

**Interfaces:**
- Produces: `EVENT_SCHEMAS["proposal.status_changed"] = {"required": ["project_id", "proposal_id", "status"], "optional": []}` — consumed by Task 2/3's `_emit_proposal_event` calls (validated implicitly the same way `proposal.ready` already is) and by Task 16's parity test indirectly (not directly, but keeps the event family consistent).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_event_schemas.py`, inside the existing
`TestExpectedEventTypes` class (find `def test_workflow_events(self):` at
line 124 and insert a new method right after its body, before
`test_timer_events_common_intervals`):

```python
    def test_proposal_events(self):
        expected = ["spec.approved", "proposal.ready", "proposal.status_changed"]
        for et in expected:
            assert et in EVENT_SCHEMAS, f"Missing schema for {et}"
```

Also append a new test class at the end of the file:

```python
class TestProposalEventSchemas:
    """Field requirements for the Phase 6 spec/proposal event family."""

    def test_proposal_status_changed_requires_status(self):
        schema = EVENT_SCHEMAS["proposal.status_changed"]
        assert schema["required"] == ["project_id", "proposal_id", "status"]
        assert schema["optional"] == []

    def test_validate_payload_accepts_committed_and_discarded(self):
        for status in ("committed", "discarded"):
            errors = validate_payload(
                "proposal.status_changed",
                {"project_id": "p1", "proposal_id": "prop-1", "status": status},
            )
            assert errors == [], errors
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/jkern/dev/agent-queue2 && pytest tests/test_event_schemas.py -v -k "proposal"`
Expected: FAIL — `"proposal.status_changed" not in EVENT_SCHEMAS` / `KeyError`.

- [ ] **Step 3: Add the schema entry**

Edit `src/event_schemas.py`, in the `_SPEC_SCHEMAS` dict (lines 747-756):

```python
_SPEC_SCHEMAS: dict[str, EventSchema] = {
    "spec.approved": {
        "required": ["project_id", "spec_path"],
        "optional": [],
    },
    "proposal.ready": {
        "required": ["project_id", "proposal_id"],
        "optional": [],
    },
    "proposal.status_changed": {
        "required": ["project_id", "proposal_id", "status"],
        "optional": [],
    },
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/jkern/dev/agent-queue2 && pytest tests/test_event_schemas.py -v -k "proposal"`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add src/event_schemas.py tests/test_event_schemas.py
git commit -m "feat(events): register proposal.status_changed schema"
```

---

### Task 2: Emit `proposal.status_changed` from `task_batch_discard`

**Files:**
- Modify: `src/commands/proposal_commands.py:183-198` (`_cmd_task_batch_discard`)
- Test: `tests/test_task_batch_commands.py`

**Interfaces:**
- Consumes: `TaskProposalCommandsMixin._emit_proposal_event(event_type: str, payload: dict) -> None` (already exists, `src/commands/proposal_commands.py:94-101`); `EVENT_SCHEMAS["proposal.status_changed"]` from Task 1.
- Produces: every successful `task_batch_discard` call now also emits `("proposal.status_changed", {"project_id": ..., "proposal_id": ..., "status": "discarded"})` on `self.orchestrator.bus`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_task_batch_commands.py` (after `test_discard`, using
the existing `handler` fixture and `_emitted(h)` helper already defined
in that file at lines 73-94):

```python
async def test_discard_emits_status_changed_event(handler):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    prop = await handler.execute(
        "task_batch_propose",
        {
            "project_id": "p1",
            "source": "spec:foo",
            "tasks": [{"tempId": "a", "title": "A", "description": ""}],
            "edges": [],
        },
    )
    prop_id = prop["proposal_id"]
    r = await handler.execute("task_batch_discard", {"proposal_id": prop_id})
    assert r["success"] is True
    events = [e for e in _emitted(handler) if e[0] == "proposal.status_changed"]
    assert events and events[-1][1] == {
        "project_id": "p1",
        "proposal_id": prop_id,
        "status": "discarded",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2 && pytest tests/test_task_batch_commands.py -v -k test_discard_emits_status_changed_event`
Expected: FAIL — `events` is empty (`assert events and ...` fails on the empty list).

- [ ] **Step 3: Emit the event in `_cmd_task_batch_discard`**

Edit `src/commands/proposal_commands.py`, lines 195-198:

```python
        await proposal_queries.update_proposal(
            self.db, proposal_id, status="discarded"
        )
        await self._emit_proposal_event(
            "proposal.status_changed",
            {
                "project_id": row["project_id"],
                "proposal_id": proposal_id,
                "status": "discarded",
            },
        )
        return {"success": True}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2 && pytest tests/test_task_batch_commands.py -v -k discard`
Expected: PASS (both `test_discard` and `test_discard_emits_status_changed_event`).

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add src/commands/proposal_commands.py tests/test_task_batch_commands.py
git commit -m "feat(proposals): emit proposal.status_changed on discard"
```

---

### Task 3: Emit `proposal.status_changed` from `task_batch_commit`

**Files:**
- Modify: `src/commands/proposal_commands.py:329-331` (`_cmd_task_batch_commit`, success return)
- Test: `tests/test_task_batch_commands.py`

**Interfaces:**
- Consumes: same `_emit_proposal_event` helper as Task 2.
- Produces: every successful `task_batch_commit` call emits
  `("proposal.status_changed", {"project_id": ..., "proposal_id": ..., "status": "committed"})`.
  Rollback (failure) path does **not** emit — the proposal reverts to
  `"ready"`, not a terminal state, matching the per-pane spec §8.2's
  scope ("on success").

- [ ] **Step 1: Write the failing test**

Append to `tests/test_task_batch_commands.py`:

```python
async def test_commit_emits_status_changed_event(handler):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    prop = await handler.execute(
        "task_batch_propose",
        {
            "project_id": "p1",
            "source": "spec:foo",
            "tasks": [{"tempId": "a", "title": "A", "description": ""}],
            "edges": [],
        },
    )
    prop_id = prop["proposal_id"]
    r = await handler.execute("task_batch_commit", {"proposal_id": prop_id})
    assert r["success"] is True
    events = [e for e in _emitted(handler) if e[0] == "proposal.status_changed"]
    assert events and events[-1][1] == {
        "project_id": "p1",
        "proposal_id": prop_id,
        "status": "committed",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2 && pytest tests/test_task_batch_commands.py -v -k test_commit_emits_status_changed_event`
Expected: FAIL — empty `events` list.

- [ ] **Step 3: Emit the event before the success return**

Edit `src/commands/proposal_commands.py`, line 331 (the final line of
`_cmd_task_batch_commit`, currently `return {"success": True, "task_ids": created_ids}`):

```python
        await self._emit_proposal_event(
            "proposal.status_changed",
            {
                "project_id": project_id,
                "proposal_id": proposal_id,
                "status": "committed",
            },
        )
        return {"success": True, "task_ids": created_ids}
```

(`project_id` is already a local variable in this method, assigned at
line 231: `project_id: str = row["project_id"]`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2 && pytest tests/test_task_batch_commands.py -v`
Expected: PASS — all tests in the file, including the two new ones and
the pre-existing rollback/concurrency tests (confirms the new emit call
doesn't run on the rollback path).

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add src/commands/proposal_commands.py tests/test_task_batch_commands.py
git commit -m "feat(proposals): emit proposal.status_changed on commit"
```

---

### Task 4: Add `ProposalStatusChangedEvent` to the dashboard WS type union

**Files:**
- Modify: `dashboard/src/ws/types.ts`
- Test: none (pure type-level change; exercised indirectly by Task 5's test and by `npm run typecheck`)

**Interfaces:**
- Produces: `export interface ProposalStatusChangedEvent extends BaseEvent { event_type: "proposal.status_changed"; project_id: string; proposal_id: string; status: "committed" | "discarded"; }`, added to the `NotifyEvent` union — consumed by Task 5.

- [ ] **Step 1: Add the interface**

Edit `dashboard/src/ws/types.ts`, inserting a new section right before
`// --- Union type ---` (currently line 352):

```ts
// --- Proposal lifecycle (Phase 6 spec-ingest follow-up) ---

export interface ProposalStatusChangedEvent extends BaseEvent {
  event_type: "proposal.status_changed";
  project_id: string;
  proposal_id: string;
  status: "committed" | "discarded";
}

// --- Union type ---
```

- [ ] **Step 2: Add it to the `NotifyEvent` union**

Edit the union (currently ending at line 392 with `| CommandInvokedEvent;`):

```ts
export type NotifyEvent =
  | TaskStartedEvent
  | TaskCompletedEvent
  | TaskFailedEvent
  | TaskBlockedEvent
  | TaskStoppedEvent
  | AgentQuestionEvent
  | PlanAwaitingApprovalEvent
  | PRCreatedEvent
  | MergeConflictEvent
  | PushFailedEvent
  | BudgetWarningEvent
  | SystemOnlineEvent
  | TaskThreadOpenEvent
  | TaskMessageEvent
  | TaskThreadCloseEvent
  | TextNotifyEvent
  | ChainStuckEvent
  | StuckDefinedTaskEvent
  | PlaybookRunStartedEvent
  | PlaybookRunCompletedEvent
  | PlaybookRunFailedEvent
  | PlaybookRunPausedEvent
  | PlaybookRunResumedEvent
  | PlaybookRunTimedOutEvent
  | PlaybookCompilationSucceededEvent
  | PlaybookCompilationFailedEvent
  | GateCreatedEvent
  | GateResolvedEvent
  | GateExpiredEvent
  | MessageSentEvent
  | MessageDeliveredEvent
  | MessageRepliedEvent
  | SessionStartedEvent
  | SessionExitedEvent
  | SessionAdoptedEvent
  | TaskBlockedGraphEvent
  | TaskUnblockedEvent
  | CommandInvokedEvent
  | ProposalStatusChangedEvent;
```

- [ ] **Step 3: Typecheck**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npm run typecheck`
Expected: no new errors (this file has no other consumers yet — Task 5
adds the first one).

- [ ] **Step 4: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/ws/types.ts
git commit -m "feat(dashboard): add ProposalStatusChangedEvent WS type"
```

---

### Task 5: Bootstrap Vitest + React Testing Library

The dashboard has zero test tooling today (no `vitest`/`jest` config, no
test script, no `*.test.*` files). Every remaining frontend task in this
plan needs it, so it lands now, before the WS invalidation test.

**Files:**
- Modify: `dashboard/package.json`
- Modify: `dashboard/vite.config.ts`
- Create: `dashboard/src/test-setup.ts`
- Test: `dashboard/src/__tests__/smoke.test.tsx`

**Interfaces:**
- Produces: `npm run test` (from `dashboard/`) running `vitest run`;
  a jsdom environment with `@testing-library/jest-dom` matchers and a
  `ResizeObserver` stub (required by `@xyflow/react`, used in Task 13/14)
  available to every test file.

- [ ] **Step 1: Add dependencies**

Edit `dashboard/package.json`, `devDependencies`:

```json
    "@testing-library/jest-dom": "^6.6.0",
    "@testing-library/react": "^16.1.0",
    "@testing-library/user-event": "^14.5.2",
    "jsdom": "^25.0.1",
    "vitest": "^3.0.0",
```

(insert alphabetically among the existing `devDependencies` entries)
and add a `"test"` script next to the existing ones:

```json
    "test": "vitest run",
```

- [ ] **Step 2: Install**

Run: `cd /home/jkern/dev/agent-queue2 && npm install -w dashboard -D @testing-library/jest-dom @testing-library/react @testing-library/user-event jsdom vitest`
Expected: installs cleanly, `dashboard/package.json` and the root
`package-lock.json` update.

- [ ] **Step 3: Wire Vitest into the Vite config**

Edit `dashboard/vite.config.ts` — change the `defineConfig` import source
and add a `test` block:

```ts
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Read via globalThis so this config typechecks without @types/node.
const target =
  (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env
    .AQ_API_TARGET ?? "http://127.0.0.1:8081";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": target,
      "/health": target,
      "/ready": target,
      "/ws": { target, ws: true },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    globals: false,
  },
});
```

- [ ] **Step 4: Create the test setup file**

Create `dashboard/src/test-setup.ts`:

```ts
import "@testing-library/jest-dom/vitest";

// @xyflow/react measures node containers via ResizeObserver, which
// jsdom does not implement. Every test that renders a <ReactFlow>
// (proposal-preview's graph section, Task 13/14) needs this stub.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

(globalThis as unknown as { ResizeObserver: typeof ResizeObserverStub }).ResizeObserver =
  ResizeObserverStub;
```

- [ ] **Step 5: Write a smoke test**

Create `dashboard/src/__tests__/smoke.test.tsx`:

```tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

describe("test tooling smoke test", () => {
  it("renders a component and asserts on its text", () => {
    render(<div>hello from vitest</div>);
    expect(screen.getByText("hello from vitest")).toBeInTheDocument();
  });
});
```

- [ ] **Step 6: Run it**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/__tests__/smoke.test.tsx`
Expected: PASS (1 test).

- [ ] **Step 7: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/package.json dashboard/vite.config.ts dashboard/src/test-setup.ts dashboard/src/__tests__/smoke.test.tsx package-lock.json
git commit -m "chore(dashboard): bootstrap vitest + testing-library"
```

---

### Task 6: WS invalidation branch for `proposal.status_changed`

**Files:**
- Modify: `dashboard/src/ws/useEventStream.ts`
- Test: `dashboard/src/ws/__tests__/useEventStream.test.tsx`

**Interfaces:**
- Consumes: `ProposalStatusChangedEvent` from Task 4.
- Produces: any `proposal.status_changed` frame invalidates the
  `["proposal", proposalId]` React Query key — consumed indirectly by
  Task 12's `useProposal` hook once wired into a live app.

- [ ] **Step 1: Write the failing test**

Create `dashboard/src/ws/__tests__/useEventStream.test.tsx`:

```tsx
import { describe, it, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useEventStream } from "../useEventStream";
import type { ProposalStatusChangedEvent } from "../types";

function wrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe("useEventStream — proposal.status_changed", () => {
  it("invalidates the proposal detail query", () => {
    const client = new QueryClient();
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useEventStream(), { wrapper: wrapper(client) });

    const event: ProposalStatusChangedEvent = {
      _event_type: "proposal.status_changed",
      event_type: "proposal.status_changed",
      severity: "info",
      category: "proposal",
      project_id: "p1",
      proposal_id: "prop-abc",
      status: "committed",
    };

    // useEventStream exposes no direct dispatch — exercise it the same
    // way the module-level WS socket would, by calling the internal
    // handler indirectly through onEvent (the hook always subscribes
    // its returned handleEvent to the module listener set; here we
    // invoke onEvent directly via the options callback wiring, which
    // the hook forwards to the same code path as a live WS frame).
    expect(result.current).toBeUndefined(); // useEventStream returns void
    // Simulate a frame by calling the same handler useEventStream
    // registers — re-render with onEvent capturing the call, proving
    // the hook is wired; the invalidation assertion below exercises
    // the real prefix-branch logic directly.
    void invalidateSpy;
    void event;
  });
});
```

This first draft doesn't actually reach the internal `handleEvent` (it's
not exported) — replace it with a direct unit test against the
invalidation logic instead, since `useEventStream`'s WS singleton makes
end-to-end simulation awkward for a unit test. Rewrite Step 1 as follows
before running it:

```tsx
import { describe, it, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useEventStream } from "../useEventStream";
import type { ProposalStatusChangedEvent } from "../types";

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe("useEventStream — proposal.status_changed", () => {
  it("invalidates the proposal detail query on a proposal.status_changed frame", () => {
    const client = new QueryClient();
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    let captured: ((e: ProposalStatusChangedEvent) => void) | undefined;

    renderHook(
      () =>
        useEventStream({
          onEvent: (e) => {
            captured = captured ?? ((_: ProposalStatusChangedEvent) => {});
            void e;
          },
        }),
      { wrapper: makeWrapper(client) },
    );

    // useEventStream's handleEvent is registered on the module-level
    // listener set as a side effect of the hook running; invoke it the
    // same way the WS onmessage callback does by re-implementing the
    // minimal dispatch this test needs to prove: call the hook's
    // returned handler indirectly is not possible (it's not exposed),
    // so this test instead asserts the documented contract at the
    // integration boundary — see Step 3's manual note.
    void invalidateSpy;
  });
});
```

**Stop — this approach is fighting the module's encapsulation.**
`useEventStream`'s `handleEvent` is a private `useCallback`, not exposed
for direct invocation, and the WS socket is a module-level singleton
with no test seam. Use the same technique the codebase would use for
this: export a tiny event-dispatch helper for tests. Add one export to
`useEventStream.ts` itself (see Step 2) and test against that directly —
this is the actually-correct Step 1. Replace the file above with:

```tsx
import { describe, it, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useEventStream, __dispatchEventForTests } from "../useEventStream";
import type { ProposalStatusChangedEvent } from "../types";

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe("useEventStream — proposal.status_changed", () => {
  it("invalidates the proposal detail query on a proposal.status_changed frame", () => {
    const client = new QueryClient();
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    renderHook(() => useEventStream(), { wrapper: makeWrapper(client) });

    const event: ProposalStatusChangedEvent = {
      _event_type: "proposal.status_changed",
      event_type: "proposal.status_changed",
      severity: "info",
      category: "proposal",
      project_id: "p1",
      proposal_id: "prop-abc",
      status: "committed",
    };
    __dispatchEventForTests(event);

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["proposal", "prop-abc"] });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/ws/__tests__/useEventStream.test.tsx`
Expected: FAIL — `__dispatchEventForTests` is not exported.

- [ ] **Step 3: Add the test-only dispatch export and the invalidation branch**

Edit `dashboard/src/ws/useEventStream.ts`. First, export a thin wrapper
around the existing module-level `eventListeners` set (add right after
the `eventListeners`/`statusListeners` declarations, around line 28):

```ts
/** Test-only: push a synthetic frame through the same listener set the
 *  real WebSocket's onmessage handler uses. Not used by production code. */
export function __dispatchEventForTests(event: NotifyEvent): void {
  for (const fn of eventListeners) fn(event);
}
```

Then add the import and the new prefix branch. Update the top import
(line 11):

```ts
import type { NotifyEvent, TaskMessageEvent, ProposalStatusChangedEvent } from "./types";
```

And insert a new branch in `handleEvent` right after the existing
`task.blocked` / `task.unblocked` block (after line 211, before the
`switch (type)` on line 213):

```ts
      if (type === "proposal.status_changed") {
        const pid = (event as ProposalStatusChangedEvent).proposal_id;
        queryClient.invalidateQueries({ queryKey: ["proposal", pid] });
        return;
      }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/ws/__tests__/useEventStream.test.tsx`
Expected: PASS.

- [ ] **Step 5: Typecheck**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npm run typecheck`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/ws/useEventStream.ts dashboard/src/ws/__tests__/useEventStream.test.tsx
git commit -m "feat(dashboard): invalidate proposal query on proposal.status_changed"
```

---

### Task 7: `zod` dependency + shared pane types module

**Files:**
- Modify: `dashboard/package.json`
- Create: `dashboard/src/panes/types.ts`
- Test: `dashboard/src/panes/__tests__/types.test.ts`

**Interfaces:**
- Produces: `PaneManifest<TArgs>`, `PaneToolbarAction`, `ShortcutBinding`,
  `PaneViewProps<TArgs>`, `HeroIcon` — all exported from
  `dashboard/src/panes/types.ts`. Consumed by every subsequent frontend
  task (`manifest.ts`, `registry.ts`, `useShellPane.ts`, `index.tsx`).

- [ ] **Step 1: Add the `zod` dependency**

Edit `dashboard/package.json`, `dependencies` (insert alphabetically):

```json
    "zod": "^3.24.1",
```

Run: `cd /home/jkern/dev/agent-queue2 && npm install -w dashboard zod`
Expected: installs cleanly.

- [ ] **Step 2: Write a failing type-usage test**

Create `dashboard/src/panes/__tests__/types.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { z } from "zod";
import type { PaneManifest, PaneViewProps, PaneToolbarAction, ShortcutBinding } from "../types";

describe("pane types module", () => {
  it("PaneManifest accepts a minimal valid shape", () => {
    const argsSchema = z.object({ id: z.string() });
    const manifest: PaneManifest<{ id: string }> = {
      id: "example",
      name: "Example",
      description: "An example pane.",
      icon: () => null,
      args_schema: argsSchema,
      agent_pushable: true,
      palette_label: "Open example",
      palette_section: "Examples",
    };
    expect(manifest.id).toBe("example");
    expect(manifest.args_schema?.safeParse({ id: "x" }).success).toBe(true);
  });

  it("open_shortcut defaults to undefined, not null, when omitted", () => {
    const manifest: PaneManifest = {
      id: "example",
      name: "Example",
      description: "",
      icon: () => null,
    };
    expect(manifest.open_shortcut).toBeUndefined();
  });

  it("PaneViewProps, PaneToolbarAction, ShortcutBinding compose without error", () => {
    const action: PaneToolbarAction = { id: "a", label: "A", onClick: () => {} };
    const binding: ShortcutBinding = { key: "r", label: "Refresh", onFire: () => {} };
    const props: PaneViewProps<{ id: string }> = {
      args: { id: "x" },
      close: () => {},
      setArgs: () => {},
      setToolbar: () => {},
      setShortcuts: () => {},
    };
    expect(props.args.id).toBe("x");
    expect(action.id).toBe("a");
    expect(binding.key).toBe("r");
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/__tests__/types.test.ts`
Expected: FAIL — cannot find module `../types`.

- [ ] **Step 4: Create the types module**

Create `dashboard/src/panes/types.ts`:

```ts
import type { ComponentType, SVGProps } from "react";
import type { z } from "zod";

/** This dashboard is standardized on heroicons — see dashboard/CLAUDE.md
 *  and pane-plugin interface spec §4. Never introduce LucideIcon. */
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

  /** zod schema for the args object. `undefined` means "no args required". */
  args_schema?: z.ZodType<TArgs>;

  /**
   * Optional keyboard shortcut that OPENS this view. Omit the field
   * entirely (leave it `undefined`) when a view has no open shortcut —
   * never assign a literal `null`.
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

  /** Palette action label. `null`/omitted means "not registered as a palette action". */
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

  /** Register toolbar action buttons to appear in the pane header. Passing `[]` clears the toolbar. */
  setToolbar: (actions: PaneToolbarAction[]) => void;

  /** Register per-entity shortcuts scoped to this pane's focus. */
  setShortcuts: (bindings: ShortcutBinding[]) => void;
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/__tests__/types.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/package.json dashboard/src/panes/types.ts dashboard/src/panes/__tests__/types.test.ts package-lock.json
git commit -m "feat(dashboard): add zod dependency and shared pane-view types"
```

---

### Task 8: `proposal-preview/index.tsx` skeleton

Builds the minimal real component first so `manifest.ts` (Task 9) has a
valid default export to import — `manifest.ts` imports `./index`, so
`index.tsx` must exist before it.

**Files:**
- Create: `dashboard/src/panes/proposal-preview/index.tsx`
- Test: `dashboard/src/panes/proposal-preview/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `PaneViewProps<TArgs>` from `dashboard/src/panes/types.ts` (Task 7).
- Produces: `export interface ProposalPreviewArgs { proposalId: string }`
  and `export default function ProposalPreviewPane(props: PaneViewProps<ProposalPreviewArgs>)`
  — consumed by Task 9's `manifest.ts` (`import ProposalPreviewPane, { type ProposalPreviewArgs } from "./index"`).

- [ ] **Step 1: Write the failing test**

Create `dashboard/src/panes/proposal-preview/__tests__/index.test.tsx`:

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import ProposalPreviewPane from "../index";

function renderPane(overrides: Partial<Parameters<typeof ProposalPreviewPane>[0]> = {}) {
  const props = {
    args: { proposalId: "prop-abc123" },
    close: vi.fn(),
    setArgs: vi.fn(),
    setToolbar: vi.fn(),
    setShortcuts: vi.fn(),
    ...overrides,
  };
  render(<ProposalPreviewPane {...props} />);
  return props;
}

describe("ProposalPreviewPane skeleton", () => {
  it("renders the proposal id from args", () => {
    renderPane();
    expect(screen.getByText("prop-abc123")).toBeInTheDocument();
  });

  it("calls close when the close button is clicked", () => {
    const props = renderPane();
    fireEvent.click(screen.getByText("Close"));
    expect(props.close).toHaveBeenCalledTimes(1);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/proposal-preview/__tests__/index.test.tsx`
Expected: FAIL — cannot find module `../index`.

- [ ] **Step 3: Write the skeleton component**

Create `dashboard/src/panes/proposal-preview/index.tsx`:

```tsx
import { useEffect } from "react";
import type { PaneViewProps } from "../types";

export interface ProposalPreviewArgs {
  proposalId: string;
}

export default function ProposalPreviewPane({
  args,
  close,
  setToolbar,
  setShortcuts,
}: PaneViewProps<ProposalPreviewArgs>) {
  useEffect(() => {
    setToolbar([]);
  }, [setToolbar]);

  useEffect(() => {
    setShortcuts([]);
  }, [setShortcuts]);

  return (
    <div
      className="flex h-full flex-col gap-3 p-3 text-sm text-gray-200"
      data-testid="proposal-preview-pane"
    >
      <div className="font-mono text-xs opacity-70">{args.proposalId}</div>
      <button type="button" onClick={close} className="self-start text-xs underline">
        Close
      </button>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/proposal-preview/__tests__/index.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/panes/proposal-preview/index.tsx dashboard/src/panes/proposal-preview/__tests__/index.test.tsx
git commit -m "feat(dashboard): proposal-preview pane skeleton"
```

---

### Task 9: `proposal-preview/manifest.ts`

**Files:**
- Create: `dashboard/src/panes/proposal-preview/manifest.ts`
- Test: `dashboard/src/panes/proposal-preview/__tests__/manifest.test.ts`

**Interfaces:**
- Consumes: `PaneManifest` (Task 7), `ProposalPreviewPane` +
  `ProposalPreviewArgs` (Task 8).
- Produces: `export const manifest: PaneManifest<ProposalPreviewArgs>`,
  `export const argsSchema: z.ZodType<ProposalPreviewArgs>`,
  `export const Component = ProposalPreviewPane` — consumed by Task 10's
  `registry.ts` (via `import.meta.glob`).

- [ ] **Step 1: Write the failing test**

Create `dashboard/src/panes/proposal-preview/__tests__/manifest.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { manifest, argsSchema } from "../manifest";

describe("proposal-preview manifest", () => {
  it("id matches the directory name", () => {
    expect(manifest.id).toBe("proposal-preview");
  });

  it("accepts a valid proposalId", () => {
    expect(argsSchema.safeParse({ proposalId: "prop-abc" }).success).toBe(true);
  });

  it("rejects a missing or empty proposalId", () => {
    expect(argsSchema.safeParse({}).success).toBe(false);
    expect(argsSchema.safeParse({ proposalId: "" }).success).toBe(false);
  });

  it("has no open_shortcut (declared by omission, not null)", () => {
    expect(manifest.open_shortcut).toBeUndefined();
  });

  it("is agent-pushable", () => {
    expect(manifest.agent_pushable).toBe(true);
  });

  it("carries the palette label and section", () => {
    expect(manifest.palette_label).toBe("Preview proposal");
    expect(manifest.palette_section).toBe("Proposals");
  });

  it("route_scope is cross-route", () => {
    expect(manifest.route_scope).toBe("cross-route");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/proposal-preview/__tests__/manifest.test.ts`
Expected: FAIL — cannot find module `../manifest`.

- [ ] **Step 3: Write the manifest**

Create `dashboard/src/panes/proposal-preview/manifest.ts`:

```ts
import { z } from "zod";
import { DocumentMagnifyingGlassIcon } from "@heroicons/react/24/outline";
import type { PaneManifest } from "../types";
import ProposalPreviewPane, { type ProposalPreviewArgs } from "./index";

export const argsSchema: z.ZodType<ProposalPreviewArgs> = z.object({
  proposalId: z.string().min(1),
});

export const manifest: PaneManifest<ProposalPreviewArgs> = {
  id: "proposal-preview",
  name: "Proposal Preview",
  description: "Preview a staged task-batch proposal before approving it.",
  icon: DocumentMagnifyingGlassIcon,
  args_schema: argsSchema,
  route_scope: "cross-route",
  agent_pushable: true,
  palette_label: "Preview proposal",
  palette_section: "Proposals",
};

export const Component = ProposalPreviewPane;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/proposal-preview/__tests__/manifest.test.ts`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/panes/proposal-preview/manifest.ts dashboard/src/panes/proposal-preview/__tests__/manifest.test.ts
git commit -m "feat(dashboard): proposal-preview pane manifest"
```

---

### Task 10: Frontend pane registry (`dashboard/src/panes/registry.ts`)

**Files:**
- Create: `dashboard/src/panes/registry.ts`
- Test: `dashboard/src/panes/__tests__/registry.test.ts`

**Interfaces:**
- Consumes: `PaneManifest`, `PaneViewProps` (Task 7); every
  `dashboard/src/panes/*/manifest.ts` via `import.meta.glob` (currently
  only Task 9's `proposal-preview/manifest.ts`).
- Produces: `export interface PaneEntry { manifest: PaneManifest; Component: ComponentType<PaneViewProps>; }`
  and `export const PANE_REGISTRY: Record<string, PaneEntry>` — consumed
  by Task 11's `useShellPane.ts`.

- [ ] **Step 1: Write the failing test**

Create `dashboard/src/panes/__tests__/registry.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { PANE_REGISTRY } from "../registry";

describe("PANE_REGISTRY", () => {
  it("resolves every declared view", () => {
    expect(Object.keys(PANE_REGISTRY).length).toBeGreaterThan(0);
    for (const [id, entry] of Object.entries(PANE_REGISTRY)) {
      expect(entry.manifest.id).toBe(id);
      expect(entry.Component).toBeDefined();
    }
  });

  it("includes proposal-preview", () => {
    expect(PANE_REGISTRY["proposal-preview"]).toBeDefined();
    expect(PANE_REGISTRY["proposal-preview"]?.manifest.name).toBe("Proposal Preview");
  });

  it("has no open_shortcut collisions", () => {
    const seen = new Set<string>();
    for (const entry of Object.values(PANE_REGISTRY)) {
      const sc = entry.manifest.open_shortcut;
      if (!sc) continue;
      expect(seen.has(sc)).toBe(false);
      seen.add(sc);
    }
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/__tests__/registry.test.ts`
Expected: FAIL — cannot find module `../registry`.

- [ ] **Step 3: Write the registry**

Create `dashboard/src/panes/registry.ts`:

```ts
import type { ComponentType } from "react";
import type { PaneManifest, PaneViewProps } from "./types";

export interface PaneEntry {
  manifest: PaneManifest;
  Component: ComponentType<PaneViewProps>;
}

interface PaneModule {
  manifest: PaneManifest;
  Component: ComponentType<PaneViewProps>;
}

const modules = import.meta.glob<PaneModule>("./*/manifest.ts", { eager: true });

function buildRegistry(): Record<string, PaneEntry> {
  const registry: Record<string, PaneEntry> = {};
  const usedShortcuts = new Set<string>();

  for (const [path, mod] of Object.entries(modules)) {
    const { manifest, Component } = mod;
    const dirId = path.split("/").at(-2);
    if (manifest.id !== dirId) {
      throw new Error(
        `pane manifest id "${manifest.id}" does not match its directory "${dirId}"`,
      );
    }
    if (registry[manifest.id]) {
      throw new Error(`duplicate pane view id "${manifest.id}"`);
    }
    if (manifest.open_shortcut) {
      if (usedShortcuts.has(manifest.open_shortcut)) {
        throw new Error(
          `pane view "${manifest.id}" open_shortcut "${manifest.open_shortcut}" collides with another view`,
        );
      }
      usedShortcuts.add(manifest.open_shortcut);
    }
    if (!Component) {
      throw new Error(`pane view "${manifest.id}" has no default Component export`);
    }
    registry[manifest.id] = { manifest, Component };
  }

  return registry;
}

export const PANE_REGISTRY: Record<string, PaneEntry> = buildRegistry();
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/__tests__/registry.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/panes/registry.ts dashboard/src/panes/__tests__/registry.test.ts
git commit -m "feat(dashboard): pane view registry (import.meta.glob aggregator)"
```

---

### Task 11: `useShellPane` (minimal, real implementation)

Per Deviation 1: the full `ShellPane` host component is out of scope.
This is only the pane-state store contract from shell spec §5.1 — the
part `proposal-preview`'s "View spec source" toolbar action needs to
open another pane view.

**Files:**
- Create: `dashboard/src/shell/useShellPane.ts`
- Test: `dashboard/src/shell/__tests__/useShellPane.test.tsx`

**Interfaces:**
- Consumes: `PANE_REGISTRY` (Task 10).
- Produces: `export type PaneState = { kind: "closed" } | { kind: "open"; view: string; args: unknown; width: number }`
  and `export function useShellPane(): { state: PaneState; open: (viewId: string, args: unknown) => void; close: () => void }`
  — consumed by Task 15's `index.tsx` (`shellPane.open("spec-doc-reader", { path })`).

- [ ] **Step 1: Write the failing test**

Create `dashboard/src/shell/__tests__/useShellPane.test.tsx`:

```tsx
import { describe, it, expect, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useShellPane } from "../useShellPane";

describe("useShellPane", () => {
  beforeEach(() => {
    const { result } = renderHook(() => useShellPane());
    act(() => result.current.close());
  });

  it("starts closed", () => {
    const { result } = renderHook(() => useShellPane());
    expect(result.current.state.kind).toBe("closed");
  });

  it("opens a known view with valid args", () => {
    const { result } = renderHook(() => useShellPane());
    act(() => result.current.open("proposal-preview", { proposalId: "prop-abc" }));
    expect(result.current.state).toEqual({
      kind: "open",
      view: "proposal-preview",
      args: { proposalId: "prop-abc" },
      width: 480,
    });
  });

  it("no-ops on an unknown view", () => {
    const { result } = renderHook(() => useShellPane());
    act(() => result.current.open("does-not-exist", {}));
    expect(result.current.state.kind).toBe("closed");
  });

  it("no-ops on invalid args for a known view", () => {
    const { result } = renderHook(() => useShellPane());
    act(() => result.current.open("proposal-preview", { proposalId: "" }));
    expect(result.current.state.kind).toBe("closed");
  });

  it("close resets to closed", () => {
    const { result } = renderHook(() => useShellPane());
    act(() => result.current.open("proposal-preview", { proposalId: "prop-abc" }));
    act(() => result.current.close());
    expect(result.current.state.kind).toBe("closed");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/shell/__tests__/useShellPane.test.tsx`
Expected: FAIL — cannot find module `../useShellPane`.

- [ ] **Step 3: Write the hook**

Create `dashboard/src/shell/useShellPane.ts`:

```ts
// Minimal implementation of the shell pane store (dashboard shell v2
// design spec §5.1, pane-plugin interface spec §6.1). The shell
// foundation (ShellPane host component, drawer-closing on open,
// localStorage width persistence, agent-push wiring) has not landed in
// this codebase yet (see the proposal-preview plan's Deviations §1).
// Every method a pane view calls today — `open`, `close`, `state` —
// already matches the finalized contract, so that follow-up work is
// expected to extend this module in place, not replace it.

import { useSyncExternalStore } from "react";
import { PANE_REGISTRY } from "../panes/registry";

export type PaneState =
  | { kind: "closed" }
  | { kind: "open"; view: string; args: unknown; width: number };

const DEFAULT_WIDTH = 480;

let state: PaneState = { kind: "closed" };
const listeners = new Set<() => void>();

function setState(next: PaneState): void {
  state = next;
  for (const fn of listeners) fn();
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

function getSnapshot(): PaneState {
  return state;
}

function open(viewId: string, args: unknown): void {
  const entry = PANE_REGISTRY[viewId];
  if (!entry) {
    console.error(`[useShellPane] unknown pane view "${viewId}"`);
    return;
  }
  if (entry.manifest.args_schema) {
    const result = entry.manifest.args_schema.safeParse(args);
    if (!result.success) {
      console.error(`[useShellPane] invalid args for "${viewId}"`, result.error);
      return;
    }
  }
  setState({ kind: "open", view: viewId, args, width: DEFAULT_WIDTH });
}

function close(): void {
  setState({ kind: "closed" });
}

export function useShellPane(): {
  state: PaneState;
  open: (viewId: string, args: unknown) => void;
  close: () => void;
} {
  const snapshot = useSyncExternalStore(subscribe, getSnapshot);
  return { state: snapshot, open, close };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/shell/__tests__/useShellPane.test.tsx`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/shell/useShellPane.ts dashboard/src/shell/__tests__/useShellPane.test.tsx
git commit -m "feat(dashboard): minimal useShellPane store"
```

---

### Task 12: `proposal-preview/hooks.ts` — `useProposal` + `useProposalGate`

**Files:**
- Create: `dashboard/src/panes/proposal-preview/hooks.ts`
- Test: `dashboard/src/panes/proposal-preview/__tests__/hooks.test.tsx`

**Interfaces:**
- Consumes: `legacyFetch` (`dashboard/src/api/legacy-fetch.ts`, existing),
  `useGates` (`dashboard/src/api/hooks.ts:1045`, existing).
- Produces: `export interface ProposalTask { tempId: string; title: string; description: string; priority?: number }`,
  `export interface ProposalEdge { from: string; to: string; dep_type: "blocks" | "parent_child" | "waits_for" | "conditional_blocks" | "discovered_from" }`,
  `export interface ProposalDetail { proposal_id: string; project_id: string; source: string; tasks: ProposalTask[]; edges: ProposalEdge[]; status: "draft" | "ready" | "committed" | "discarded" }`,
  `export function useProposal(proposalId: string)`,
  `export function useProposalGate(projectId: string, proposalId: string): ReturnType<typeof useGates> & { gate: GateSummary | undefined }`
  — all consumed by Task 13 (`graph.ts`) and Task 14/15 (`index.tsx`).

- [ ] **Step 1: Write the failing tests**

Create `dashboard/src/panes/proposal-preview/__tests__/hooks.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("../../../api/legacy-fetch", () => ({
  legacyFetch: vi.fn(),
}));
vi.mock("../../../api/hooks", async () => {
  const actual = await vi.importActual<typeof import("../../../api/hooks")>("../../../api/hooks");
  return { ...actual, useGates: vi.fn() };
});

import { legacyFetch } from "../../../api/legacy-fetch";
import { useGates } from "../../../api/hooks";
import { useProposal, useProposalGate } from "../hooks";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("useProposal", () => {
  beforeEach(() => vi.clearAllMocks());

  it("fetches and returns the proposal detail", async () => {
    const body = {
      proposal_id: "prop-abc",
      project_id: "p1",
      source: "spec:foo.md",
      tasks: [{ tempId: "a", title: "A", description: "" }],
      edges: [],
      status: "ready",
    };
    vi.mocked(legacyFetch).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => body,
    } as Response);

    const { result } = renderHook(() => useProposal("prop-abc"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(body);
    expect(legacyFetch).toHaveBeenCalledWith("/api/proposals/prop-abc");
  });

  it("surfaces a 404 as an error", async () => {
    vi.mocked(legacyFetch).mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({}),
    } as Response);

    const { result } = renderHook(() => useProposal("prop-missing"), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe("proposal not found");
  });
});

describe("useProposalGate", () => {
  beforeEach(() => vi.clearAllMocks());

  it("finds the gate whose await_id matches the proposal", () => {
    const gate = {
      id: "g1",
      gate_type: "routing",
      await_id: "proposal:prop-abc",
      project_id: "p1",
      title: "",
    };
    vi.mocked(useGates).mockReturnValue({
      data: [gate],
      isPending: false,
    } as unknown as ReturnType<typeof useGates>);

    const { result } = renderHook(() => useProposalGate("p1", "prop-abc"), { wrapper });
    expect(result.current.gate).toEqual(gate);
  });

  it("returns undefined when no gate matches", () => {
    vi.mocked(useGates).mockReturnValue({
      data: [],
      isPending: false,
    } as unknown as ReturnType<typeof useGates>);
    const { result } = renderHook(() => useProposalGate("p1", "prop-abc"), { wrapper });
    expect(result.current.gate).toBeUndefined();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/proposal-preview/__tests__/hooks.test.tsx`
Expected: FAIL — cannot find module `../hooks`.

- [ ] **Step 3: Write `hooks.ts`**

Create `dashboard/src/panes/proposal-preview/hooks.ts`:

```ts
import { useQuery } from "@tanstack/react-query";
import { legacyFetch } from "../../api/legacy-fetch";
import { useGates } from "../../api/hooks";

export interface ProposalTask {
  tempId: string;
  title: string;
  description: string;
  priority?: number;
}

export interface ProposalEdge {
  from: string;
  to: string;
  dep_type: "blocks" | "parent_child" | "waits_for" | "conditional_blocks" | "discovered_from";
}

export interface ProposalDetail {
  proposal_id: string;
  project_id: string;
  source: string;
  tasks: ProposalTask[];
  edges: ProposalEdge[];
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
    refetchInterval: (query) => (query.state.data?.status === "ready" ? 15_000 : false),
  });
}

export function useProposalGate(projectId: string, proposalId: string) {
  const gatesQuery = useGates({ projectId, status: "open" });
  const gate = gatesQuery.data?.find((g) => g.await_id === `proposal:${proposalId}`);
  return { ...gatesQuery, gate };
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/proposal-preview/__tests__/hooks.test.tsx`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/panes/proposal-preview/hooks.ts dashboard/src/panes/proposal-preview/__tests__/hooks.test.tsx
git commit -m "feat(dashboard): proposal-preview useProposal + useProposalGate"
```

---

### Task 13: `proposal-preview/graph.ts` + `nodes.tsx`

**Files:**
- Create: `dashboard/src/panes/proposal-preview/graph.ts`
- Create: `dashboard/src/panes/proposal-preview/nodes.tsx`
- Test: `dashboard/src/panes/proposal-preview/__tests__/graph.test.ts`

**Interfaces:**
- Consumes: `ProposalTask`, `ProposalEdge` (Task 12).
- Produces: `export interface ProposalTaskNodeData { kind: "task"; tempId: string; title: string; depCount: number }`,
  `export interface ProposalGhostNodeData { kind: "ghost"; taskId: string }`,
  `export type ProposalNode = Node<ProposalTaskNodeData, "proposal-task"> | Node<ProposalGhostNodeData, "proposal-ghost">`,
  `export function layoutProposalGraph(tasksIn: ProposalTask[], edgesIn: ProposalEdge[]): { nodes: ProposalNode[]; edges: Edge[] }`
  from `graph.ts`; `export const proposalNodeTypes` (a `NodeTypes` map for
  `<ReactFlow nodeTypes={...}>`) from `nodes.tsx` — both consumed by
  Task 14's `index.tsx`.

- [ ] **Step 1: Write the failing tests**

Create `dashboard/src/panes/proposal-preview/__tests__/graph.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { layoutProposalGraph } from "../graph";

describe("layoutProposalGraph", () => {
  it("creates one node per proposed task", () => {
    const { nodes } = layoutProposalGraph(
      [
        { tempId: "a", title: "A", description: "" },
        { tempId: "b", title: "B", description: "" },
      ],
      [{ from: "a", to: "b", dep_type: "blocks" }],
    );
    expect(nodes.map((n) => n.id).sort()).toEqual(["a", "b"]);
  });

  it("adds a ghost node for an edge endpoint that isn't a proposed task", () => {
    const { nodes } = layoutProposalGraph(
      [{ tempId: "a", title: "A", description: "" }],
      [{ from: "a", to: "existing-task-123", dep_type: "blocks" }],
    );
    const ghost = nodes.find((n) => n.id === "existing-task-123");
    expect(ghost?.type).toBe("proposal-ghost");
  });

  it("computes dep count per task from outgoing edges", () => {
    const { nodes } = layoutProposalGraph(
      [
        { tempId: "a", title: "A", description: "" },
        { tempId: "b", title: "B", description: "" },
        { tempId: "c", title: "C", description: "" },
      ],
      [
        { from: "a", to: "b", dep_type: "blocks" },
        { from: "a", to: "c", dep_type: "blocks" },
      ],
    );
    const nodeA = nodes.find((n) => n.id === "a");
    expect(nodeA?.data).toMatchObject({ depCount: 2 });
  });

  it("produces one xyflow edge per proposal edge", () => {
    const { edges } = layoutProposalGraph(
      [
        { tempId: "a", title: "A", description: "" },
        { tempId: "b", title: "B", description: "" },
      ],
      [{ from: "a", to: "b", dep_type: "blocks" }],
    );
    expect(edges).toHaveLength(1);
    expect(edges[0]).toMatchObject({ source: "a", target: "b" });
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/proposal-preview/__tests__/graph.test.ts`
Expected: FAIL — cannot find module `../graph`.

- [ ] **Step 3: Write `graph.ts`**

Create `dashboard/src/panes/proposal-preview/graph.ts`:

```ts
import dagre from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/react";
import type { ProposalTask, ProposalEdge } from "./hooks";

export const PROPOSAL_NODE_WIDTH = 180;
export const PROPOSAL_NODE_HEIGHT = 56;

export interface ProposalTaskNodeData extends Record<string, unknown> {
  kind: "task";
  tempId: string;
  title: string;
  depCount: number;
}

export interface ProposalGhostNodeData extends Record<string, unknown> {
  kind: "ghost";
  taskId: string;
}

export type ProposalNode =
  | Node<ProposalTaskNodeData, "proposal-task">
  | Node<ProposalGhostNodeData, "proposal-ghost">;

export function layoutProposalGraph(
  tasksIn: ProposalTask[],
  edgesIn: ProposalEdge[],
): { nodes: ProposalNode[]; edges: Edge[] } {
  const dg = new dagre.graphlib.Graph();
  dg.setDefaultEdgeLabel(() => ({}));
  // Tighter spacing than Command Center's Graph tab (nodesep 40/ranksep
  // 100) — proposal graphs are smaller (~10-30 nodes) and the pane is
  // narrower than the full canvas.
  dg.setGraph({ rankdir: "TB", nodesep: 32, ranksep: 64 });

  const taskIds = new Set(tasksIn.map((t) => t.tempId));
  const ghostIds = new Set<string>();
  for (const e of edgesIn) {
    if (!taskIds.has(e.from)) ghostIds.add(e.from);
    if (!taskIds.has(e.to)) ghostIds.add(e.to);
  }

  const depCounts = new Map<string, number>();
  for (const e of edgesIn) {
    depCounts.set(e.from, (depCounts.get(e.from) ?? 0) + 1);
  }

  for (const t of tasksIn) {
    dg.setNode(t.tempId, { width: PROPOSAL_NODE_WIDTH, height: PROPOSAL_NODE_HEIGHT });
  }
  for (const gid of ghostIds) {
    dg.setNode(gid, { width: PROPOSAL_NODE_WIDTH, height: PROPOSAL_NODE_HEIGHT });
  }
  for (const e of edgesIn) {
    dg.setEdge(e.from, e.to);
  }

  dagre.layout(dg);

  const taskNodes: ProposalNode[] = tasksIn.map((t) => {
    const pos = dg.node(t.tempId);
    return {
      id: t.tempId,
      type: "proposal-task",
      position: {
        x: (pos?.x ?? 0) - PROPOSAL_NODE_WIDTH / 2,
        y: (pos?.y ?? 0) - PROPOSAL_NODE_HEIGHT / 2,
      },
      data: { kind: "task", tempId: t.tempId, title: t.title, depCount: depCounts.get(t.tempId) ?? 0 },
    };
  });

  const ghostNodes: ProposalNode[] = [...ghostIds].map((gid) => {
    const pos = dg.node(gid);
    return {
      id: gid,
      type: "proposal-ghost",
      position: {
        x: (pos?.x ?? 0) - PROPOSAL_NODE_WIDTH / 2,
        y: (pos?.y ?? 0) - PROPOSAL_NODE_HEIGHT / 2,
      },
      data: { kind: "ghost", taskId: gid },
    };
  });

  const edges: Edge[] = edgesIn.map((e) => ({
    id: `${e.from}->${e.to}:${e.dep_type}`,
    source: e.from,
    target: e.to,
    type: e.dep_type === "blocks" ? "smoothstep" : "default",
    animated: e.dep_type === "waits_for",
  }));

  return { nodes: [...taskNodes, ...ghostNodes], edges };
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/proposal-preview/__tests__/graph.test.ts`
Expected: PASS (4 tests).

- [ ] **Step 5: Write the node components (no dedicated test file — exercised by Task 14's component tests via rendering)**

Create `dashboard/src/panes/proposal-preview/nodes.tsx`:

```tsx
import { Handle, Position, type NodeProps, type Node, type NodeTypes } from "@xyflow/react";
import type { ProposalTaskNodeData, ProposalGhostNodeData } from "./graph";

type ProposalTaskNodeType = Node<ProposalTaskNodeData, "proposal-task">;
type ProposalGhostNodeType = Node<ProposalGhostNodeData, "proposal-ghost">;

export function ProposalTaskNode({ data, selected }: NodeProps<ProposalTaskNodeType>) {
  return (
    <div
      className={`rounded border border-sky-600 bg-sky-950 p-2 text-[11px] text-sky-100 ${
        selected ? "outline outline-2 outline-white" : ""
      }`}
      style={{ width: 180 }}
      data-testid="proposal-graph-task-node"
    >
      <Handle type="target" position={Position.Top} />
      <div className="line-clamp-2 font-medium">{data.title}</div>
      {data.depCount > 0 && (
        <div className="mt-1 text-[10px] opacity-70">
          {data.depCount} dep{data.depCount === 1 ? "" : "s"}
        </div>
      )}
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

export function ProposalGhostNode({ data }: NodeProps<ProposalGhostNodeType>) {
  return (
    <div
      className="rounded border border-dashed border-gray-600 bg-gray-900/60 p-2 text-[10px] text-gray-400"
      style={{ width: 180 }}
      data-testid="proposal-graph-ghost-node"
    >
      <Handle type="target" position={Position.Top} />
      <span className="font-mono">{data.taskId.slice(0, 8)}…</span>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

export const proposalNodeTypes: NodeTypes = {
  "proposal-task": ProposalTaskNode,
  "proposal-ghost": ProposalGhostNode,
};
```

- [ ] **Step 6: Typecheck**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npm run typecheck`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/panes/proposal-preview/graph.ts dashboard/src/panes/proposal-preview/nodes.tsx dashboard/src/panes/proposal-preview/__tests__/graph.test.ts
git commit -m "feat(dashboard): proposal-preview graph layout + node components"
```

---

### Task 14: `index.tsx` — wire real data (header, graph, list, loading/error/terminal states)

Replaces Task 8's skeleton body with the real component. Toolbar/actions
still register `[]` here — Task 15 adds them.

**Files:**
- Modify: `dashboard/src/panes/proposal-preview/index.tsx`
- Modify: `dashboard/src/panes/proposal-preview/__tests__/index.test.tsx` (replace the skeleton tests with the full suite below)

**Interfaces:**
- Consumes: `useProposal`, `useProposalGate`, `ProposalTask` (Task 12);
  `layoutProposalGraph` (Task 13); `proposalNodeTypes` (Task 13).
- Produces: same `ProposalPreviewArgs` / default export signature as
  Task 8 (unchanged contract, richer body) — consumed by Task 15
  (further edits to the same file) and by Task 10's registry (already
  wired, no change needed).

- [ ] **Step 1: Replace the test file with the full-data test suite**

Overwrite `dashboard/src/panes/proposal-preview/__tests__/index.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import ProposalPreviewPane from "../index";
import { useProposal, useProposalGate } from "../hooks";

vi.mock("../hooks", async () => {
  const actual = await vi.importActual<typeof import("../hooks")>("../hooks");
  return { ...actual, useProposal: vi.fn(), useProposalGate: vi.fn() };
});

function baseProps() {
  return {
    args: { proposalId: "prop-abc123" },
    close: vi.fn(),
    setArgs: vi.fn(),
    setToolbar: vi.fn(),
    setShortcuts: vi.fn(),
  };
}

function readyProposal() {
  return {
    proposal_id: "prop-abc123",
    project_id: "p1",
    source: "spec:projects/foo/specs/2026-08-21-thing.md",
    status: "ready" as const,
    tasks: [
      { tempId: "a", title: "setup-schema", description: "Add table", priority: 100 },
      { tempId: "b", title: "propose-commands", description: "batch commands", priority: 90 },
    ],
    edges: [{ from: "a", to: "b", dep_type: "blocks" as const }],
  };
}

describe("ProposalPreviewPane", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows a loading state while the proposal query is pending", () => {
    vi.mocked(useProposal).mockReturnValue({
      isPending: true,
      isError: false,
      data: undefined,
    } as unknown as ReturnType<typeof useProposal>);
    vi.mocked(useProposalGate).mockReturnValue({ gate: undefined } as unknown as ReturnType<
      typeof useProposalGate
    >);
    render(<ProposalPreviewPane {...baseProps()} />);
    expect(screen.getByTestId("proposal-preview-loading")).toBeInTheDocument();
  });

  it("shows the not-found state on error, and Retry/Close work", () => {
    const refetch = vi.fn();
    vi.mocked(useProposal).mockReturnValue({
      isPending: false,
      isError: true,
      data: undefined,
      refetch,
    } as unknown as ReturnType<typeof useProposal>);
    vi.mocked(useProposalGate).mockReturnValue({ gate: undefined } as unknown as ReturnType<
      typeof useProposalGate
    >);
    const props = baseProps();
    render(<ProposalPreviewPane {...props} />);
    expect(screen.getByTestId("proposal-preview-error")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Retry"));
    expect(refetch).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText("Close"));
    expect(props.close).toHaveBeenCalledTimes(1);
  });

  it("renders the header (id, status pill, source line, age) for a ready proposal", () => {
    vi.mocked(useProposal).mockReturnValue({
      isPending: false,
      isError: false,
      data: readyProposal(),
    } as unknown as ReturnType<typeof useProposal>);
    vi.mocked(useProposalGate).mockReturnValue({
      gate: { id: "g1", created_at: Date.now() / 1000 - 240 },
    } as unknown as ReturnType<typeof useProposalGate>);
    render(<ProposalPreviewPane {...baseProps()} />);
    expect(screen.getByText("prop-abc123")).toBeInTheDocument();
    expect(screen.getByText("ready")).toBeInTheDocument();
    expect(screen.getByText(/projects\/foo\/specs\/2026-08-21-thing\.md/)).toBeInTheDocument();
    expect(screen.getByText(/proposed 4m ago/)).toBeInTheDocument();
  });

  it("renders the graph container with a node per proposed task", () => {
    vi.mocked(useProposal).mockReturnValue({
      isPending: false,
      isError: false,
      data: readyProposal(),
    } as unknown as ReturnType<typeof useProposal>);
    vi.mocked(useProposalGate).mockReturnValue({ gate: undefined } as unknown as ReturnType<
      typeof useProposalGate
    >);
    render(<ProposalPreviewPane {...baseProps()} />);
    expect(screen.getByTestId("proposal-graph")).toHaveAttribute("data-node-count", "2");
  });

  it("renders one task-list row per proposed task and re-orders on sort", () => {
    vi.mocked(useProposal).mockReturnValue({
      isPending: false,
      isError: false,
      data: readyProposal(),
    } as unknown as ReturnType<typeof useProposal>);
    vi.mocked(useProposalGate).mockReturnValue({ gate: undefined } as unknown as ReturnType<
      typeof useProposalGate
    >);
    render(<ProposalPreviewPane {...baseProps()} />);
    const list = screen.getByTestId("proposal-task-list");
    expect(list.querySelectorAll("li")).toHaveLength(2);
    fireEvent.click(screen.getByText("priority"));
    const firstRow = list.querySelector("li");
    expect(firstRow?.textContent).toContain("setup-schema");
  });

  it("hides the action row and shows the committed banner in the committed state", () => {
    vi.mocked(useProposal).mockReturnValue({
      isPending: false,
      isError: false,
      data: { ...readyProposal(), status: "committed" as const },
    } as unknown as ReturnType<typeof useProposal>);
    vi.mocked(useProposalGate).mockReturnValue({ gate: undefined } as unknown as ReturnType<
      typeof useProposalGate
    >);
    render(<ProposalPreviewPane {...baseProps()} />);
    expect(screen.getByText("Committed — 2 tasks created.")).toBeInTheDocument();
    expect(screen.queryByText("Approve")).not.toBeInTheDocument();
  });

  it("shows the discarded banner in the discarded state", () => {
    vi.mocked(useProposal).mockReturnValue({
      isPending: false,
      isError: false,
      data: { ...readyProposal(), status: "discarded" as const },
    } as unknown as ReturnType<typeof useProposal>);
    vi.mocked(useProposalGate).mockReturnValue({ gate: undefined } as unknown as ReturnType<
      typeof useProposalGate
    >);
    render(<ProposalPreviewPane {...baseProps()} />);
    expect(screen.getByText("Discarded — no tasks were created.")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/proposal-preview/__tests__/index.test.tsx`
Expected: FAIL — the skeleton component doesn't call `useProposal`/render
a graph/list, so `getByTestId("proposal-preview-loading")` etc. all fail.

- [ ] **Step 3: Rewrite `index.tsx` with real data wiring**

Overwrite `dashboard/src/panes/proposal-preview/index.tsx`:

```tsx
import { useMemo, useState } from "react";
import { ReactFlow, Background, Controls, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { PaneViewProps } from "../types";
import { useProposal, useProposalGate, type ProposalTask } from "./hooks";
import { layoutProposalGraph } from "./graph";
import { proposalNodeTypes } from "./nodes";

export interface ProposalPreviewArgs {
  proposalId: string;
}

type SortKey = "title" | "priority";

const STATUS_TONE: Record<string, string> = {
  draft: "bg-gray-700 text-gray-200",
  ready: "bg-amber-600 text-amber-50",
  committed: "bg-emerald-700 text-emerald-50",
  discarded: "bg-red-900 text-red-200 line-through",
};

function stripSpecPrefix(source: string): string {
  return source.startsWith("spec:") ? source.slice("spec:".length) : source;
}

function relativeAge(epochSeconds: number): string {
  const mins = Math.max(0, Math.round((Date.now() - epochSeconds * 1000) / 60_000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  return `${Math.round(mins / 60)}h ago`;
}

export default function ProposalPreviewPane({
  args,
  close,
  setToolbar,
  setShortcuts,
}: PaneViewProps<ProposalPreviewArgs>) {
  const [sortKey, setSortKey] = useState<SortKey>("title");
  const [selectedTempId, setSelectedTempId] = useState<string | null>(null);

  const proposalQuery = useProposal(args.proposalId);
  const data = proposalQuery.data;
  const projectId = data?.project_id ?? "";
  const gateQuery = useProposalGate(projectId, args.proposalId);
  const gate = gateQuery.gate;

  const sortedTasks: ProposalTask[] = useMemo(() => {
    const tasksList = data?.tasks ?? [];
    const copy = [...tasksList];
    if (sortKey === "title") copy.sort((a, b) => a.title.localeCompare(b.title));
    else copy.sort((a, b) => (b.priority ?? 100) - (a.priority ?? 100));
    return copy;
  }, [data?.tasks, sortKey]);

  const { nodes, edges } = useMemo(
    () => layoutProposalGraph(data?.tasks ?? [], data?.edges ?? []),
    [data?.tasks, data?.edges],
  );

  const displayNodes: Node[] = useMemo(
    () => nodes.map((n) => ({ ...n, selected: n.id === selectedTempId })),
    [nodes, selectedTempId],
  );

  const sourcePath = data ? stripSpecPrefix(data.source) : "";
  const age = gate?.created_at ? relativeAge(gate.created_at) : null;

  // Toolbar and shortcuts are wired up in the next task; register empty
  // arrays for now so the pane cleans up correctly on unmount.
  setToolbar([]);
  setShortcuts([]);

  if (proposalQuery.isPending) {
    return (
      <div className="flex h-full flex-col gap-3 p-3" data-testid="proposal-preview-loading">
        <div className="h-4 w-40 animate-pulse rounded bg-white/10" />
        <div className="h-[280px] animate-pulse rounded bg-white/5" />
        <div className="h-4 w-full animate-pulse rounded bg-white/10" />
        <div className="h-4 w-full animate-pulse rounded bg-white/10" />
        <div className="h-4 w-full animate-pulse rounded bg-white/10" />
      </div>
    );
  }

  if (proposalQuery.isError || !data) {
    return (
      <div
        className="flex h-full flex-col items-center justify-center gap-3 p-3 text-sm text-gray-300"
        data-testid="proposal-preview-error"
      >
        <div>Proposal not found (or failed to load)</div>
        <div className="font-mono text-xs opacity-70">{args.proposalId}</div>
        <div className="flex gap-2">
          <button type="button" className="btn-secondary" onClick={() => proposalQuery.refetch()}>
            Retry
          </button>
          <button type="button" className="btn-secondary" onClick={close}>
            Close
          </button>
        </div>
      </div>
    );
  }

  return (
    <div
      className="flex h-full flex-col gap-3 overflow-y-auto p-3 text-sm text-gray-200"
      data-testid="proposal-preview-pane"
    >
      <header className="flex flex-col gap-1 border-b border-white/10 pb-3">
        <div className="flex items-center justify-between">
          <span className="truncate font-mono text-xs" title={data.proposal_id}>
            {data.proposal_id}
          </span>
          <span className={`rounded px-2 py-0.5 text-[10px] uppercase tracking-wide ${STATUS_TONE[data.status]}`}>
            {data.status}
          </span>
        </div>
        <div className="truncate text-xs opacity-80">from spec: {sourcePath}</div>
        {age && <div className="text-xs opacity-60">proposed {age}</div>}
      </header>

      <div
        className="h-[280px] shrink-0 rounded border border-white/10"
        data-testid="proposal-graph"
        data-node-count={nodes.length}
      >
        <ReactFlow
          nodes={displayNodes}
          edges={edges}
          nodeTypes={proposalNodeTypes}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={true}
          fitView
          panOnScroll
          onNodeClick={(_evt, node) => setSelectedTempId(node.id)}
        >
          <Background />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>

      <div className="flex items-center justify-between">
        <div className="text-xs font-medium opacity-80">Proposed tasks ({data.tasks.length})</div>
        <div className="flex gap-2 text-xs">
          <button
            type="button"
            className={sortKey === "title" ? "underline" : "opacity-60"}
            onClick={() => setSortKey("title")}
          >
            title
          </button>
          <button
            type="button"
            className={sortKey === "priority" ? "underline" : "opacity-60"}
            onClick={() => setSortKey("priority")}
          >
            priority
          </button>
        </div>
      </div>

      <ul data-testid="proposal-task-list" className="flex flex-col divide-y divide-white/5">
        {sortedTasks.map((t) => (
          <li
            key={t.tempId}
            role="button"
            tabIndex={0}
            className={`flex cursor-pointer items-center justify-between gap-2 py-1.5 text-xs ${
              selectedTempId === t.tempId ? "bg-white/5" : ""
            }`}
            title={t.description}
            onClick={() => setSelectedTempId(t.tempId)}
            onKeyDown={(e) => {
              if (e.key === "Enter") setSelectedTempId(t.tempId);
            }}
          >
            <span className="truncate">{t.title}</span>
            <span className="shrink-0 opacity-60">P{t.priority ?? 100}</span>
            <span className="shrink-0 opacity-40">—</span>
          </li>
        ))}
      </ul>

      {data.status === "committed" && (
        <div className="border-t border-white/10 pt-3 text-xs text-emerald-300">
          Committed — {data.tasks.length} tasks created.
        </div>
      )}
      {data.status === "discarded" && (
        <div className="border-t border-white/10 pt-3 text-xs text-red-300">
          Discarded — no tasks were created.
        </div>
      )}
    </div>
  );
}
```

Note: `setToolbar([])`/`setShortcuts([])` are called directly in the
render body here (not inside a `useEffect`) as an interim state — Task
15 moves them into properly-dependent `useEffect` calls once there is
real content to register. Calling a state setter passed as a prop during
render is safe here because `setToolbar`/`setShortcuts` in the test
mocks are plain `vi.fn()` (no state update), and in Task 15 they move
into effects before this ships to a real shell.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/proposal-preview/__tests__/index.test.tsx`
Expected: PASS (7 tests).

- [ ] **Step 5: Typecheck**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npm run typecheck`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/panes/proposal-preview/index.tsx dashboard/src/panes/proposal-preview/__tests__/index.test.tsx
git commit -m "feat(dashboard): proposal-preview pane — header, graph, task list"
```

---

### Task 15: `index.tsx` — actions, toolbar, shortcuts

**Files:**
- Modify: `dashboard/src/panes/proposal-preview/index.tsx`
- Modify: `dashboard/src/panes/proposal-preview/__tests__/index.test.tsx` (append tests)

**Interfaces:**
- Consumes: `useResolveGate` (`dashboard/src/api/hooks.ts:1081`, existing),
  `legacyFetch` (existing), `useShellPane` (Task 11).
- Produces: final `ProposalPreviewPane` behavior — approve/discard
  actions, toolbar (`refresh`, `view-source`), shortcuts (`r`, `s`, and
  `a`/`d` only when `status === "ready"`).

- [ ] **Step 1: Add the failing tests**

Append the following `it` blocks inside the existing `describe("ProposalPreviewPane", ...)`
block in `dashboard/src/panes/proposal-preview/__tests__/index.test.tsx`,
just before its closing `});`. First add the new imports/mocks at the
top of the file (alongside the existing `vi.mock("../hooks", ...)`):

```tsx
vi.mock("../../../api/hooks", async () => {
  const actual = await vi.importActual<typeof import("../../../api/hooks")>("../../../api/hooks");
  return { ...actual, useResolveGate: vi.fn() };
});
vi.mock("../../../api/legacy-fetch", () => ({ legacyFetch: vi.fn() }));
```

and add these imports below the existing `useProposal, useProposalGate`
import:

```tsx
import { useResolveGate } from "../../../api/hooks";
import { legacyFetch } from "../../../api/legacy-fetch";
```

Then append these tests:

```tsx
  it("registers exactly two toolbar actions; view-source is disabled without a spec path", () => {
    vi.mocked(useProposal).mockReturnValue({
      isPending: false,
      isError: false,
      data: { ...readyProposal(), source: "not-a-spec-path" },
    } as unknown as ReturnType<typeof useProposal>);
    vi.mocked(useProposalGate).mockReturnValue({ gate: undefined } as unknown as ReturnType<
      typeof useProposalGate
    >);
    const props = baseProps();
    render(<ProposalPreviewPane {...props} />);
    const lastCall = props.setToolbar.mock.calls.at(-1)?.[0];
    expect(lastCall).toHaveLength(2);
    expect(lastCall.map((a: { id: string }) => a.id)).toEqual(["refresh", "view-source"]);
    expect(lastCall.find((a: { id: string }) => a.id === "view-source").disabled).toBe(true);
  });

  it("registers a/d shortcuts only when status is ready", () => {
    vi.mocked(useProposal).mockReturnValue({
      isPending: false,
      isError: false,
      data: readyProposal(),
    } as unknown as ReturnType<typeof useProposal>);
    vi.mocked(useProposalGate).mockReturnValue({ gate: undefined } as unknown as ReturnType<
      typeof useProposalGate
    >);
    const props = baseProps();
    render(<ProposalPreviewPane {...props} />);
    const lastCall = props.setShortcuts.mock.calls.at(-1)?.[0];
    expect(lastCall.map((b: { key: string }) => b.key)).toEqual(["r", "s", "a", "d"]);
  });

  it("excludes a/d shortcuts when status is committed", () => {
    vi.mocked(useProposal).mockReturnValue({
      isPending: false,
      isError: false,
      data: { ...readyProposal(), status: "committed" as const },
    } as unknown as ReturnType<typeof useProposal>);
    vi.mocked(useProposalGate).mockReturnValue({ gate: undefined } as unknown as ReturnType<
      typeof useProposalGate
    >);
    const props = baseProps();
    render(<ProposalPreviewPane {...props} />);
    const lastCall = props.setShortcuts.mock.calls.at(-1)?.[0];
    expect(lastCall.map((b: { key: string }) => b.key)).toEqual(["r", "s"]);
  });

  it("clicking Approve with a resolved gate calls resolveGate then close", async () => {
    vi.mocked(useProposal).mockReturnValue({
      isPending: false,
      isError: false,
      data: readyProposal(),
    } as unknown as ReturnType<typeof useProposal>);
    const gate = { id: "gate-1", created_at: Date.now() / 1000 };
    vi.mocked(useProposalGate).mockReturnValue({ gate } as unknown as ReturnType<typeof useProposalGate>);
    const mutateAsync = vi.fn().mockResolvedValue({ success: true });
    vi.mocked(useResolveGate).mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useResolveGate>);
    const props = baseProps();
    render(<ProposalPreviewPane {...props} />);
    fireEvent.click(screen.getByText("Approve"));
    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith({
        gate_id: "gate-1",
        resolved_by: "dashboard",
        resolution: "approved",
      }),
    );
    await waitFor(() => expect(props.close).toHaveBeenCalledTimes(1));
  });

  it("Approve is disabled and a no-op when no gate is found", () => {
    vi.mocked(useProposal).mockReturnValue({
      isPending: false,
      isError: false,
      data: readyProposal(),
    } as unknown as ReturnType<typeof useProposal>);
    vi.mocked(useProposalGate).mockReturnValue({ gate: undefined } as unknown as ReturnType<
      typeof useProposalGate
    >);
    const mutateAsync = vi.fn();
    vi.mocked(useResolveGate).mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useResolveGate>);
    render(<ProposalPreviewPane {...baseProps()} />);
    expect(screen.getByText("Approve")).toBeDisabled();
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("Discard requires confirmation, then calls the discard mutation and closes", async () => {
    vi.mocked(useProposal).mockReturnValue({
      isPending: false,
      isError: false,
      data: readyProposal(),
    } as unknown as ReturnType<typeof useProposal>);
    vi.mocked(useProposalGate).mockReturnValue({ gate: undefined } as unknown as ReturnType<
      typeof useProposalGate
    >);
    vi.mocked(legacyFetch).mockResolvedValue({
      ok: true,
      json: async () => ({ success: true }),
    } as Response);
    const props = baseProps();
    render(<ProposalPreviewPane {...props} />);
    fireEvent.click(screen.getByText("Discard"));
    fireEvent.click(screen.getByText("Yes"));
    await waitFor(() =>
      expect(legacyFetch).toHaveBeenCalledWith(
        "/api/commands/execute",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    await waitFor(() => expect(props.close).toHaveBeenCalledTimes(1));
  });

  it("a failed approve renders an inline error banner and does not close", async () => {
    vi.mocked(useProposal).mockReturnValue({
      isPending: false,
      isError: false,
      data: readyProposal(),
    } as unknown as ReturnType<typeof useProposal>);
    const gate = { id: "gate-1", created_at: Date.now() / 1000 };
    vi.mocked(useProposalGate).mockReturnValue({ gate } as unknown as ReturnType<typeof useProposalGate>);
    const mutateAsync = vi.fn().mockRejectedValue(new Error("boom"));
    vi.mocked(useResolveGate).mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useResolveGate>);
    const props = baseProps();
    render(<ProposalPreviewPane {...props} />);
    fireEvent.click(screen.getByText("Approve"));
    await waitFor(() => expect(screen.getByText("Approve failed: boom")).toBeInTheDocument());
    expect(props.close).not.toHaveBeenCalled();
  });
```

Also add `waitFor` to the existing `@testing-library/react` import at the
top of the file (`import { render, screen, fireEvent, waitFor } from "@testing-library/react";`).

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/proposal-preview/__tests__/index.test.tsx`
Expected: FAIL — `setToolbar`/`setShortcuts` are called with `[]` today;
`Approve`/`Discard` buttons don't exist yet.

- [ ] **Step 3: Add actions, toolbar, and shortcuts to `index.tsx`**

Edit `dashboard/src/panes/proposal-preview/index.tsx`. Update the top
imports:

```tsx
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ReactFlow, Background, Controls, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { ArrowPathIcon, DocumentTextIcon } from "@heroicons/react/24/outline";
import type { PaneViewProps, PaneToolbarAction, ShortcutBinding } from "../types";
import { useProposal, useProposalGate, type ProposalTask } from "./hooks";
import { layoutProposalGraph } from "./graph";
import { proposalNodeTypes } from "./nodes";
import { useResolveGate } from "../../api/hooks";
import { legacyFetch } from "../../api/legacy-fetch";
import { useShellPane } from "../../shell/useShellPane";
```

Add a `looksLikeSpecPath` helper next to the existing `stripSpecPrefix`/`relativeAge` helpers:

```tsx
function looksLikeSpecPath(path: string): boolean {
  return path.length > 0 && path.endsWith(".md");
}
```

Add a module-level discard mutation hook, above the component:

```tsx
function useDiscardProposal(proposalId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const r = await legacyFetch("/api/commands/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          command: "task_batch_discard",
          args: { proposal_id: proposalId },
        }),
      });
      if (!r.ok) throw new Error(`discard failed: ${r.status}`);
      return r.json();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["proposal", proposalId] });
    },
  });
}
```

Inside the component, replace the two interim lines
`setToolbar([]);` / `setShortcuts([]);` (called directly in the render
body in Task 14) with the following — this block goes right after the
`const age = ...` line and before the `if (proposalQuery.isPending)`
early return:

```tsx
  const status = data?.status;
  const [confirmingDiscard, setConfirmingDiscard] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const resolveGate = useResolveGate();
  const discardProposal = useDiscardProposal(args.proposalId);
  const shellPane = useShellPane();
  const canViewSource = looksLikeSpecPath(sourcePath);

  function handleRefresh() {
    proposalQuery.refetch();
  }

  function handleViewSource() {
    if (!canViewSource) return;
    shellPane.open("spec-doc-reader", { path: sourcePath });
  }

  async function handleApprove() {
    if (!gate) return;
    setActionError(null);
    try {
      await resolveGate.mutateAsync({ gate_id: gate.id, resolved_by: "dashboard", resolution: "approved" });
      close();
    } catch (err) {
      setActionError(`Approve failed: ${(err as Error).message}`);
    }
  }

  async function handleDiscard() {
    setActionError(null);
    try {
      await discardProposal.mutateAsync();
      close();
    } catch (err) {
      setActionError(`Discard failed: ${(err as Error).message}`);
      setConfirmingDiscard(false);
    }
  }

  useEffect(() => {
    const actions: PaneToolbarAction[] = [
      { id: "refresh", label: "Refresh", icon: ArrowPathIcon, onClick: handleRefresh },
      {
        id: "view-source",
        label: "View spec source",
        icon: DocumentTextIcon,
        disabled: !canViewSource,
        onClick: handleViewSource,
      },
    ];
    setToolbar(actions);
    return () => setToolbar([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setToolbar, canViewSource, sourcePath]);

  useEffect(() => {
    const bindings: ShortcutBinding[] = [
      { key: "r", label: "Refresh", onFire: handleRefresh },
      { key: "s", label: "View spec source", onFire: handleViewSource },
    ];
    if (status === "ready") {
      bindings.push(
        { key: "a", label: "Approve", onFire: handleApprove },
        { key: "d", label: "Discard (confirm)", onFire: () => setConfirmingDiscard(true) },
      );
    }
    setShortcuts(bindings);
    return () => setShortcuts([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setShortcuts, status, canViewSource, sourcePath, gate?.id]);
```

Note `const [selectedTempId, ...]` and the other Task 14 hooks stay
above this block unchanged (all hooks remain before the early returns —
Rules of Hooks compliance is why `status`, `confirmingDiscard`,
`actionError`, `resolveGate`, `discardProposal`, `shellPane`,
`canViewSource` are declared here rather than after the `isPending`/`isError`
checks).

Finally, replace the two terminal-state blocks at the bottom of the JSX
(after the task list `<ul>`, replacing the closing `</div>` of the
component) with:

```tsx
      {data.status === "committed" && (
        <div className="border-t border-white/10 pt-3 text-xs text-emerald-300">
          Committed — {data.tasks.length} tasks created.
        </div>
      )}
      {data.status === "discarded" && (
        <div className="border-t border-white/10 pt-3 text-xs text-red-300">
          Discarded — no tasks were created.
        </div>
      )}

      {actionError && (
        <div className="rounded border border-red-800 bg-red-950/50 p-2 text-xs text-red-200">
          {actionError}
        </div>
      )}

      {data.status === "ready" && (
        <div className="flex justify-between gap-3 border-t border-white/10 pt-3">
          {confirmingDiscard ? (
            <div className="flex items-center gap-2 text-xs">
              <span>Really discard?</span>
              <button
                type="button"
                className="text-red-300 underline"
                onClick={handleDiscard}
                disabled={discardProposal.isPending}
              >
                Yes
              </button>
              <button type="button" className="opacity-70 underline" onClick={() => setConfirmingDiscard(false)}>
                No
              </button>
            </div>
          ) : (
            <button
              type="button"
              className="btn-danger-ghost"
              onClick={() => setConfirmingDiscard(true)}
              disabled={discardProposal.isPending}
            >
              {discardProposal.isPending ? "Discarding…" : "Discard"}
            </button>
          )}
          <button
            type="button"
            className="btn-primary"
            onClick={handleApprove}
            disabled={!gate || resolveGate.isPending}
            title={!gate ? "Waiting for approval gate to appear…" : undefined}
          >
            {resolveGate.isPending ? "Approving…" : "Approve"}
          </button>
        </div>
      )}
      {data.status === "ready" && !gate && (
        <div className="text-xs italic opacity-60">Waiting for approval gate to appear…</div>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/panes/proposal-preview/__tests__/index.test.tsx`
Expected: PASS (14 tests total).

- [ ] **Step 5: Run the full frontend suite + typecheck**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npm run test && npm run typecheck`
Expected: all tests pass; no type errors.

- [ ] **Step 6: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/panes/proposal-preview/index.tsx dashboard/src/panes/proposal-preview/__tests__/index.test.tsx
git commit -m "feat(dashboard): proposal-preview pane — approve/discard actions, toolbar, shortcuts"
```

---

### Task 16: Server-side pane registry mirror (`src/panes/registry.py`) + parity test

**Files:**
- Create: `src/panes/__init__.py`
- Create: `src/panes/registry.py`
- Test: `tests/test_pane_registry_parity.py`

**Interfaces:**
- Produces: `SERVER_PANE_REGISTRY: dict[str, dict[str, bool]]` — the
  server-side mirror the pane-plugin interface spec §7 describes, used
  to validate `--pane-open` frames on `aq message send` (that wiring
  itself is not part of this plan — it's tracked by the message-frame
  work referenced in the plugin-interface spec §6.5, out of scope here).

- [ ] **Step 1: Write the failing test**

Create `src/panes/__init__.py` (empty file, matching the rest of `src/`'s
package convention — every subpackage has one, confirmed via
`src/database/__init__.py`).

Create `tests/test_pane_registry_parity.py`:

```python
"""Parity check between the frontend pane-view manifests and the
server-side mirror (pane-plugin interface spec §7).
"""
from __future__ import annotations

import re
from pathlib import Path

from src.panes.registry import SERVER_PANE_REGISTRY

DASHBOARD_PANES_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "src" / "panes"

_ID_RE = re.compile(r'id:\s*"([^"]+)"')


def _read_frontend_manifest_ids() -> set[str]:
    ids: set[str] = set()
    for manifest_path in DASHBOARD_PANES_DIR.glob("*/manifest.ts"):
        text = manifest_path.read_text()
        match = _ID_RE.search(text)
        assert match, f"{manifest_path} has no `id: \"...\"` field"
        ids.add(match.group(1))
    return ids


def test_frontend_and_backend_registries_match():
    frontend_ids = _read_frontend_manifest_ids()
    backend_ids = set(SERVER_PANE_REGISTRY.keys())
    assert frontend_ids == backend_ids


def test_proposal_preview_is_agent_pushable():
    assert SERVER_PANE_REGISTRY["proposal-preview"]["agent_pushable"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jkern/dev/agent-queue2 && pytest tests/test_pane_registry_parity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.panes.registry'`.

- [ ] **Step 3: Write the registry**

Create `src/panes/registry.py`:

```python
"""Server-side mirror of the frontend pane-view registry.

Kept in sync manually with ``dashboard/src/panes/*/manifest.ts`` — see
the parity test in ``tests/test_pane_registry_parity.py`` and the
pane-plugin interface spec §7 for why this is a hand-maintained static
dict rather than a generated companion (only a handful of views are
expected; a generated companion becomes worth it if that count grows).

Used to validate ``--pane-open`` frames passed to ``aq message send``: a
frame naming a view not in this dict, or naming a view whose
``agent_pushable`` is ``False``, is rejected. (That validation wiring
itself is a separate change — not part of this file.)
"""
from __future__ import annotations

SERVER_PANE_REGISTRY: dict[str, dict[str, bool]] = {
    "proposal-preview": {"agent_pushable": True},
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jkern/dev/agent-queue2 && pytest tests/test_pane_registry_parity.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add src/panes/__init__.py src/panes/registry.py tests/test_pane_registry_parity.py
git commit -m "feat(panes): server-side pane registry mirror for proposal-preview"
```

---

### Task 17: `dashboard/src/shell/paneGateDispatch.ts` — gate-row Enter dispatch (deviation 5)

Standalone pure function implementing shell spec §6.3's dispatch-by-
`gate_type` logic for what a focused drawer gate row's `Enter` key
should open. Ships ahead of `ActivityDrawer` (which does not exist in
this codebase — see Deviations §1/§5) so the logic exists, is tested,
and is ready to be called the moment the drawer lands.

**Files:**
- Create: `dashboard/src/shell/paneGateDispatch.ts`
- Test: `dashboard/src/shell/__tests__/paneGateDispatch.test.ts`

**Interfaces:**
- Produces: `export interface GateDispatchInput { gate_type: string; await_id?: string | null; task_id?: string | null }`,
  `export type GateDispatchTarget = { view: "task-detail"; args: { taskId: string } } | { view: "proposal-preview"; args: { proposalId: string } } | null`,
  `export function resolveGateEnterTarget(gate: GateDispatchInput): GateDispatchTarget`
  — intended consumer (not built in this plan): `ActivityDrawer`'s
  Enter-key handler calling `useShellPane().open(target.view, target.args)`
  when `resolveGateEnterTarget(...)` returns non-null.

- [ ] **Step 1: Write the failing tests**

Create `dashboard/src/shell/__tests__/paneGateDispatch.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { resolveGateEnterTarget } from "../paneGateDispatch";

describe("resolveGateEnterTarget", () => {
  it("routes a routing gate with a proposal await_id to proposal-preview", () => {
    const target = resolveGateEnterTarget({
      gate_type: "routing",
      await_id: "proposal:prop-8f2a1c9d0e11",
    });
    expect(target).toEqual({ view: "proposal-preview", args: { proposalId: "prop-8f2a1c9d0e11" } });
  });

  it("routes a human gate with a task_id to task-detail", () => {
    const target = resolveGateEnterTarget({ gate_type: "human", task_id: "task-abc" });
    expect(target).toEqual({ view: "task-detail", args: { taskId: "task-abc" } });
  });

  it("falls back to task-detail for an unrecognized gate_type when a task_id is present", () => {
    const target = resolveGateEnterTarget({ gate_type: "pr-merged", task_id: "task-xyz" });
    expect(target).toEqual({ view: "task-detail", args: { taskId: "task-xyz" } });
  });

  it("returns null when nothing resolvable is present", () => {
    const target = resolveGateEnterTarget({ gate_type: "review" });
    expect(target).toBeNull();
  });

  it("does not treat a routing gate with a non-proposal await_id as a proposal", () => {
    const target = resolveGateEnterTarget({
      gate_type: "routing",
      await_id: "something-else:xyz",
      task_id: "task-fallback",
    });
    expect(target).toEqual({ view: "task-detail", args: { taskId: "task-fallback" } });
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/shell/__tests__/paneGateDispatch.test.ts`
Expected: FAIL — cannot find module `../paneGateDispatch`.

- [ ] **Step 3: Write the dispatch function**

Create `dashboard/src/shell/paneGateDispatch.ts`:

```ts
// Pure dispatch logic for "which pane view should open when a human
// presses Enter on a focused gate row in the activity drawer" (shell
// spec §6.3). Extracted standalone, dependency-free, so it can ship —
// and be tested — ahead of the ActivityDrawer component itself, which
// does not exist in this codebase yet (dashboard shell Phase B). When
// ActivityDrawer is built, its Enter-key handler should call this
// function and pass a non-null result straight to
// `useShellPane().open(target.view, target.args)`.

export interface GateDispatchInput {
  gate_type: string;
  await_id?: string | null;
  task_id?: string | null;
}

export type GateDispatchTarget =
  | { view: "task-detail"; args: { taskId: string } }
  | { view: "proposal-preview"; args: { proposalId: string } }
  | null;

const PROPOSAL_AWAIT_PREFIX = "proposal:";

export function resolveGateEnterTarget(gate: GateDispatchInput): GateDispatchTarget {
  if (gate.gate_type === "routing" && gate.await_id?.startsWith(PROPOSAL_AWAIT_PREFIX)) {
    const proposalId = gate.await_id.slice(PROPOSAL_AWAIT_PREFIX.length);
    if (proposalId) return { view: "proposal-preview", args: { proposalId } };
  }
  if (gate.task_id) {
    return { view: "task-detail", args: { taskId: gate.task_id } };
  }
  return null;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/jkern/dev/agent-queue2/dashboard && npx vitest run src/shell/__tests__/paneGateDispatch.test.ts`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/jkern/dev/agent-queue2
git add dashboard/src/shell/paneGateDispatch.ts dashboard/src/shell/__tests__/paneGateDispatch.test.ts
git commit -m "feat(dashboard): gate-row Enter dispatch for proposal-preview (pre-ActivityDrawer)"
```

---

### Task 18: Full-suite verification + manual verification checklist

**Files:** none (verification-only task).

- [ ] **Step 1: Run the full backend test suite**

Run: `cd /home/jkern/dev/agent-queue2 && pytest tests/test_event_schemas.py tests/test_task_batch_commands.py tests/test_pane_registry_parity.py -v`
Expected: all PASS.

- [ ] **Step 2: Run the full frontend test suite + typecheck + lint**

Run:
```bash
cd /home/jkern/dev/agent-queue2/dashboard
npm run test
npm run typecheck
npm run lint
```
Expected: all PASS, no type errors, no new lint errors (existing lint
warnings elsewhere in the dashboard are out of scope).

- [ ] **Step 3: Manual verification checklist**

Run the daemon locally (`./run.sh start` from repo root) and the
dashboard dev server (`npm run dev` from `dashboard/`, proxies to the
daemon). Then:

- [ ] Trigger a real spec-ingest run that produces a proposal (or call
      `task_batch_propose` directly against the running daemon via
      `aq surface exec task_batch_propose --project-id <pid> --source spec:foo.md --tasks '[...]' --edges '[]'`,
      matching the shape `tests/test_task_batch_commands.py` exercises)
      and confirm a `proposal.ready` gate appears via
      `aq gate list --status open --json`.
- [ ] Confirm `aq gate list` shows a `routing` gate with
      `await_id == "proposal:<the-proposal-id>"`.
- [ ] In a scratch browser console on the dashboard dev server (since
      `ActivityDrawer` doesn't exist yet to click through), manually
      exercise the pane: `import("/src/panes/registry.ts").then(m => console.log(m.PANE_REGISTRY))`
      confirms `"proposal-preview"` is present; mount
      `<ProposalPreviewPane args={{ proposalId: "<the-proposal-id>" }} .../>`
      in a scratch route or Storybook-less ad-hoc harness (e.g. a
      temporary route added locally, not committed) and confirm:
  - [ ] Header shows the real id, `READY` status pill, source line, age.
  - [ ] Graph renders one node per proposed task (open browser devtools,
        confirm `data-node-count` matches `aq task_batch` payload's task count).
  - [ ] Task list shows all proposed tasks; sort toggle re-orders them.
  - [ ] Clicking **Approve** resolves the gate — confirm via
        `aq gate list --status open` (gate no longer listed) — and the
        pipeline's `commit_on_gate_resolve` node fires
        `task_batch_commit`, confirmed via `aq task list --project-id <pid>`
        showing the new tasks.
  - [ ] Re-run the flow with **Discard** instead: confirm
        `aq task_batch show <proposal-id>` (or equivalent inspection)
        reflects `status: discarded` and no tasks were created.
- [ ] Confirm the `proposal.status_changed` WS frame arrives: watch
      `ws://.../ws/events` (e.g. via browser devtools Network tab) during
      the approve/discard flow above and confirm a
      `{"event_type": "proposal.status_changed", ...}` frame appears.
- [ ] Note in the PR description: `ActivityDrawer` itself, the palette
      wiring for `palette_label: "Preview proposal"`, and the agent-push
      `InlineEventCard` chip rendering are **not** built by this plan
      (Deviation 1) — they depend on the still-unbuilt shell foundation
      and should be tracked as follow-up work once that lands.

- [ ] **Step 4: Final commit (if the checklist above surfaced any fixups)**

If manual verification found issues, fix them, re-run the relevant
automated tests from Steps 1-2, then:

```bash
cd /home/jkern/dev/agent-queue2
git add -A
git commit -m "fix(dashboard): proposal-preview pane — manual verification fixups"
```

If no fixups were needed, this step is a no-op — the plan is complete as
of Task 17's commit.

---

## Self-review notes

- **Spec coverage:** every requirement in the per-pane design spec's §3
  (manifest), §5 (component/layout/header/graph/list/actions), §6
  (toolbar/shortcuts), §7 (data/queries), §8 (live-update), §9
  (loading/error/terminal states), §11 (tests) is implemented across
  Tasks 8-15. The plugin-interface spec's §7 (server mirror) is Task 16.
  The shell spec's §6.3 gate dispatch is Task 17 (as a standalone
  function, per Deviation 5). The user's 14-item task list is fully
  covered — see the mapping in Deviation 1's surrounding discussion.
- **Known non-goals carried forward from the per-pane spec (§2):** no
  proposal editor, no `proposal-list` view, no `GET /api/proposals`
  list endpoint — none of these are built here, matching the spec.
- **Placeholder scan:** no `TODO`/`TBD` markers in any task's code. Task
  6's first two test-writing attempts are deliberately shown failing and
  corrected in-line (documenting a real design decision — the WS
  singleton has no test seam — arrived at during planning) rather than
  left as a placeholder; the final, correct test is what Step 1 ends on
  and what Step 2 onward exercises.
- **Type consistency:** `ProposalPreviewArgs` (Task 8) flows unchanged
  into `manifest.ts` (Task 9, via `import type`), `registry.ts` (Task
  10, via the generic `PaneManifest`), and stays the exported name
  through Tasks 14-15. `ProposalTask`/`ProposalEdge`/`ProposalDetail`
  (Task 12) are the single source of truth consumed by `graph.ts` (Task
  13) and `index.tsx` (Task 14) — no parallel/renamed copies. `HeroIcon`
  (Task 7) is used consistently for both `PaneManifest.icon` and
  `PaneToolbarAction.icon` (Deviation 3).
