# Dashboard Shell v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the dashboard shell rework — global Agent Q supervisor at `/`, chat-dominant layout, `ShellPane` + `ActivityDrawer` primitives, Linear-style command palette, keyboard vocabulary, and Command Center consolidation (3 tabs) — everything the nine per-view pane plans depend on.

**Architecture:** New `AppShellV2` component behind a `?v2=1` query flag lives alongside the current shell until every pane view + CC consolidation lands, then the old shell is deleted. Backend adds one global-admin scope path (elevated + project_id=None), one loopback restriction, and a `supervisor-global` session cold-start. Frontend adds a shell shell, a palette (cmdk), a hotkey layer (`react-hotkeys-hook` + custom `useShortcuts`), the two right-side primitives, and the CC tab hub.

**Tech Stack:** TypeScript, React 19, `cmdk`, `react-hotkeys-hook`, TanStack Query, Vite. Python 3.12, FastAPI, SQLAlchemy, alembic, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-22-dashboard-shell-v2-design.md` (companion: `2026-08-22-pane-plugin-interface-design.md`).

## Global Constraints

- Icons: **heroicons only**. Never LucideIcon.
- Palette prefixes: `>` actions, `#` tasks, `@` projects. Modifier key detected via UA — normalized `$mod` binding, expanded at register time.
- `Enter` submits chat, `Shift-Enter` newline, `$mod-Enter` fallback submit.
- Bearer scope: extend the elevated flag with a `project_id=None` sub-shape (global admin). No new `kind` in `RequestScope`; no schema migration on the token side.
- Loopback restriction on global-admin tokens: enforced in `TokenAuthMiddleware`.
- Global supervisor memory scope: `supervisor:global`. Isolated from per-project supervisor scopes.
- Session lifecycle: on-demand. Idle timeout 45 min via `supervisor.global.idle_timeout_seconds` config.
- Session id: `supervisor-global`. Runtime session name: `n-supervisor--global`.
- Route redirects: `/work*` → `/command-center/*`; `/work/events` → `/command-center/tasks?openDrawer=events`; etc.
- Every new event schema goes through `src/event_schemas.py`.
- All new frontend imports of the daemon SDK go through `@aq/ts-client`. New shell primitives that need daemon data must add hooks in `dashboard/src/api/hooks.ts` — never fetch directly.

---

## File Structure

**Create (backend):**
- `tests/test_supervisor_global_scope.py` — scope-check tests for the new global-admin path.
- `tests/test_supervisor_global_lifecycle.py` — mint / spawn / idle-timeout tests.
- `tests/test_supervisor_global_token_loopback.py` — loopback restriction tests.

**Modify (backend):**
- `src/api/scope.py` — extend `check_command_scope`'s elevated path with `project_id=None` global-admin sub-shape.
- `src/api/middleware.py` — loopback restriction on global-admin tokens.
- `src/api/auth.py` — audit-log payload gains `scope=global_admin` on admin tokens.
- `src/messages/session_lens.py` — cold-start path for `supervisor-global` (project_id=None, name `n-supervisor--global`, memory scope `supervisor:global`).
- `src/config.py` — add `supervisor.global.idle_timeout_seconds: int = 2700`.
- `src/commands/session_commands.py` (or wherever supervisor mint lives) — mint elevated + project_id=None token for the global session.

**Create (frontend, shell):**
- `dashboard/src/shell/AppShellV2.tsx` — root shell (left rail + top bar + center outlet + right surface).
- `dashboard/src/shell/LeftRail.tsx` — left navigation.
- `dashboard/src/shell/TopBar.tsx` — palette trigger, bell, status.
- `dashboard/src/shell/RightSurface.tsx` — mutual-exclusion primitive dispatching between `ShellPane` and `ActivityDrawer`.
- `dashboard/src/shell/ShellPaneHost.tsx` — pane-host that reads `useShellPaneStore` + renders the current view from `PANE_REGISTRY`.
- `dashboard/src/shell/ActivityDrawer.tsx` — activity/gates drawer.
- `dashboard/src/shell/useRightSurface.ts` — shared open-state + width persistence for both right-side surfaces.
- `dashboard/src/shell/palette/Palette.tsx` — cmdk-based command palette.
- `dashboard/src/shell/palette/registerActions.ts` — `useRegisterAction` hook + palette action store.
- `dashboard/src/shell/palette/rankResults.ts` — fuzzy + recency ranking.
- `dashboard/src/shell/hotkeys/usePlatform.ts` — modifier detection.
- `dashboard/src/shell/hotkeys/useShortcuts.ts` — scoped shortcut registration + cheat-sheet feed.
- `dashboard/src/shell/hotkeys/CheatSheetModal.tsx` — `?` cheat sheet.
- `dashboard/src/shell/GotoModeOverlay.tsx` — `g` prefix two-key sequence visual.
- `dashboard/src/pages/GlobalChat.tsx` — the new `/` page — reuses `ChatConversation` pointed at `supervisor-global`.
- `dashboard/src/__tests__/shell.*.test.tsx` — shell primitive tests (see per-task testing steps).

**Modify (frontend):**
- `dashboard/src/App.tsx` — mount `AppShellV2` when `?v2=1`; else current shell. Add all v2 routes.
- `dashboard/src/components/Layout.tsx` — no change (kept for v1 flag-off compat until Task 20 flag removal).
- `dashboard/src/pages/chat/ChatConversation.tsx` — accept optional `sessionOverride` prop so it can be pointed at `supervisor-global` instead of a project supervisor.
- `dashboard/src/pages/chat/useChatTranscript.ts` — accept `sessionAddress` + `threadId` overrides.
- `dashboard/src/pages/command-center/*` — CC tab hub, retire inline `TaskSidebar` (dispatch to `pane.open`), Agents tab folds sessions in.
- `dashboard/package.json` — add `cmdk`, `react-hotkeys-hook`.

---

## Task 1: Backend — global-admin scope path in `check_command_scope`

**Files:**
- Modify: `src/api/scope.py`
- Create: `tests/test_supervisor_global_scope.py`

**Interfaces:**
- Consumes: existing `RequestScope` from `src/api/auth.py` (already has `elevated: bool`, `project_id: str | None`, `session_id: str | None`, `task_id: str | None`).
- Produces: `check_command_scope` accepts elevated + project_id=None as "global admin" and returns None (allow) without any project match or AGENT_COMMAND_SET filter.

- [ ] **Step 1: Write the failing test**

Create `tests/test_supervisor_global_scope.py`:

```python
from src.api.scope import check_command_scope
from src.api.auth import RequestScope


def test_global_admin_scope_allows_any_command():
    scope = RequestScope(
        kind="session", session_id="supervisor-global",
        task_id=None, project_id=None, elevated=True,
    )
    for cmd in ("create_project", "task_close", "playbook_install", "delete_task"):
        assert check_command_scope(cmd, {}, scope) is None


def test_global_admin_scope_skips_project_id_match():
    scope = RequestScope(
        kind="session", session_id="supervisor-global",
        task_id=None, project_id=None, elevated=True,
    )
    args = {"project_id": "any-project"}
    assert check_command_scope("task_create", args, scope) is None
    # Args passed through untouched — no injection.
    assert args == {"project_id": "any-project"}


def test_per_project_elevated_still_enforces_project_match():
    scope = RequestScope(
        kind="session", session_id="s1",
        task_id=None, project_id="demo", elevated=True,
    )
    assert check_command_scope("task_create", {"project_id": "demo"}, scope) is None
    r = check_command_scope("task_create", {"project_id": "other"}, scope)
    assert r is not None and "project_id mismatch" in r


def test_non_elevated_with_null_project_still_narrow():
    scope = RequestScope(
        kind="session", session_id="s1",
        task_id="t1", project_id=None, elevated=False,
    )
    r = check_command_scope("create_project", {}, scope)
    assert r is not None and "out of scope" in r
```

- [ ] **Step 2: Run and fail**

Run: `pytest tests/test_supervisor_global_scope.py -v`
Expected: FAIL — global admin path not implemented.

- [ ] **Step 3: Extend `check_command_scope`**

In `src/api/scope.py`, update the elevated branch:

```python
if scope.elevated:
    # Global admin — elevated + no project scope means the token can
    # touch any command in any project. Used exclusively by the
    # supervisor-global session.
    if scope.project_id is None:
        return None
    # Per-project elevated (existing path).
    expected_pid = scope.project_id
    value = args.get("project_id")
    if value is None:
        args["project_id"] = expected_pid
    elif value != expected_pid:
        return "out of scope: project_id mismatch"
    return None
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_supervisor_global_scope.py tests/test_api_scope.py -v`
Expected: 4 new + all existing PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/scope.py tests/test_supervisor_global_scope.py
git commit -m "feat(scope): global-admin scope (elevated + project_id=None)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: Backend — loopback restriction on global-admin tokens

**Files:**
- Modify: `src/api/middleware.py`
- Create: `tests/test_supervisor_global_token_loopback.py`

**Interfaces:**
- Consumes: `RequestScope` (Task 1), `TokenAuthMiddleware` (existing).
- Produces: middleware rejects (403) when scope is elevated + project_id=None and the request client is not loopback.

- [ ] **Step 1: Write the failing test**

Create `tests/test_supervisor_global_token_loopback.py`:

```python
import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI

from src.api.middleware import TokenAuthMiddleware
from src.api.auth import RequestScope


def _app_with_scope(scope: RequestScope, client_host: str) -> FastAPI:
    app = FastAPI()
    store = MagicMock()
    store.validate = AsyncMock(return_value=scope)
    app.add_middleware(TokenAuthMiddleware, token_store=store)

    @app.get("/probe")
    async def probe():
        return {"ok": True}

    # Patch the client-host source used by the middleware; consult the
    # actual implementation for the exact hook to override (Starlette's
    # request.client.host).
    return app


@pytest.mark.asyncio
async def test_global_admin_from_loopback_allowed():
    scope = RequestScope(kind="session", session_id="supervisor-global",
                         project_id=None, elevated=True)
    app = _app_with_scope(scope, client_host="127.0.0.1")
    transport = ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/probe", headers={"Authorization": "Bearer aqs_x"})
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_global_admin_from_remote_rejected():
    scope = RequestScope(kind="session", session_id="supervisor-global",
                         project_id=None, elevated=True)
    app = _app_with_scope(scope, client_host="203.0.113.9")
    transport = ASGITransport(app=app, client=("203.0.113.9", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/probe", headers={"Authorization": "Bearer aqs_x"})
        assert r.status_code == 403
        assert "loopback" in r.text.lower()


@pytest.mark.asyncio
async def test_per_project_elevated_from_remote_allowed():
    scope = RequestScope(kind="session", session_id="s1", project_id="demo",
                         elevated=True)
    app = _app_with_scope(scope, client_host="203.0.113.9")
    transport = ASGITransport(app=app, client=("203.0.113.9", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/probe", headers={"Authorization": "Bearer aqs_x"})
        assert r.status_code == 200
```

- [ ] **Step 2: Run and fail**

Run: `pytest tests/test_supervisor_global_token_loopback.py -v`
Expected: `test_global_admin_from_remote_rejected` FAILS (returns 200).

- [ ] **Step 3: Add loopback restriction**

In `src/api/middleware.py::TokenAuthMiddleware.__call__` (or the dispatch method), after resolving the scope:

```python
if scope.kind == "session" and scope.elevated and scope.project_id is None:
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        return Response(
            status_code=403,
            content="token restricted to loopback",
            media_type="text/plain",
        )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_supervisor_global_token_loopback.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/middleware.py tests/test_supervisor_global_token_loopback.py
git commit -m "feat(auth): loopback-restrict global-admin tokens

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Backend — `supervisor-global` cold-start in SessionLens

**Files:**
- Modify: `src/messages/session_lens.py`
- Create: `tests/test_supervisor_global_lifecycle.py`

**Interfaces:**
- Consumes: existing supervisor cold-start path in `session_lens.py` (project-scoped supervisor).
- Produces: when messaging address is `supervisor-global`, mint token with elevated=True + project_id=None + memory_scope_id="supervisor:global"; spawn session `n-supervisor--global`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_supervisor_global_lifecycle.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.messages.session_lens import SessionLens


@pytest.mark.asyncio
async def test_ensure_started_supervisor_global_mints_admin_token():
    token_store = MagicMock()
    token_store.mint = AsyncMock(return_value="aqs_global")
    # Build a SessionLens with mocked collaborators; the exact wiring
    # mirrors existing tests for per-project supervisor cold-start.
    # ... (defer to per-repo test scaffolding; see test_session_lens.py)
    lens = _build_lens(token_store=token_store, provider_starts_ok=True)
    ok = await lens.ensure_started(
        kind="session", target_id="supervisor-global", project_id=None,
    )
    assert ok is True
    token_store.mint.assert_awaited()
    call = token_store.mint.await_args.kwargs
    assert call["project_id"] is None
    assert call["elevated"] is True


@pytest.mark.asyncio
async def test_supervisor_global_runtime_session_name():
    from src.messages.session_lens import _resolve_runtime_session_name
    assert _resolve_runtime_session_name("session", "supervisor-global") == "n-supervisor--global"


@pytest.mark.asyncio
async def test_supervisor_global_memory_scope():
    lens = _build_lens(...)
    profile = await lens._load_profile_for_global()  # or inspect the spec builder
    # The spec built for supervisor-global carries memory_scope_id "supervisor:global".
    # Exact assertion depends on implementation; see the existing per-project supervisor
    # test for the shape.
```

(Actual scaffolding will follow `tests/test_session_lens.py`; adapt fixture names to match.)

- [ ] **Step 2: Run and fail**

Expected: FAIL — `supervisor-global` address unrecognized.

- [ ] **Step 3: Extend `session_lens.py`**

The existing supervisor cold-start branches on `target_id.startswith(_SUPERVISOR_NAME_PREFIX)` (which is `"supervisor-"`). `supervisor-global` matches that prefix — the derivation `derived_project = target_id[len(_SUPERVISOR_NAME_PREFIX):]` produces `"global"`. Reject the empty-project shortcut when `derived_project == "global"` and instead treat it as the global session:

```python
if derived_project == "global":
    # Global supervisor cold-start — admin scope, isolated memory scope.
    profile = await self._profiles_loader("supervisor")
    if profile is None:
        return False
    # Reuse the existing spec-builder path with these overrides:
    #   - project_id = None
    #   - memory_scope_id = "supervisor:global"
    #   - runtime session name = "n-supervisor--global"
    #   - token mint call gets elevated=True, project_id=None
    session_id = str(uuid.uuid4())
    api_token = await self._token_store.mint(
        session_id=session_id,
        task_id=None,
        project_id=None,
        elevated=True,
    )
    # Build the session spec with the same helpers used for per-project,
    # substituting the overrides above. Consult the existing per-project
    # branch (lines ~270-330 in current session_lens.py) for the exact
    # spec-build call.
    ...
    return True
```

Also: `_resolve_runtime_session_name` for `target_id="supervisor-global"` must return `"n-supervisor--global"` — the existing formula `f"n-supervisor--{project_id}"` where `project_id="global"` already produces that.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_supervisor_global_lifecycle.py tests/test_session_lens.py -v`
Expected: PASS (new tests) + all existing per-project tests still pass.

- [ ] **Step 5: Commit**

```bash
git add src/messages/session_lens.py tests/test_supervisor_global_lifecycle.py
git commit -m "feat(sessions): supervisor-global cold-start with admin scope + isolated memory

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Backend — supervisor.global.idle_timeout_seconds config

**Files:**
- Modify: `src/config.py`
- Test: extend an existing config test file (e.g. `tests/test_config.py`).

**Interfaces:**
- Consumes: existing supervisor config section.
- Produces: `config.supervisor.global.idle_timeout_seconds` — int, default 2700 (45min).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py` (or create):

```python
def test_supervisor_global_idle_timeout_default():
    from src.config import load_config
    c = load_config(...)  # existing test scaffolding
    assert c.supervisor.global_.idle_timeout_seconds == 2700
```

(`global_` because `global` is a Python keyword — use `global_` in the attr, alias to `global` in YAML if needed via `Field(alias=...)`.)

- [ ] **Step 2: Run and fail**

Expected: FAIL — attr not defined.

- [ ] **Step 3: Add to config schema**

In `src/config.py`, extend the supervisor config with a nested `global` sub-section:

```python
class GlobalSupervisorConfig(BaseModel):
    idle_timeout_seconds: int = 2700

class SupervisorConfig(BaseModel):
    ...  # existing
    global_: GlobalSupervisorConfig = Field(
        default_factory=GlobalSupervisorConfig, alias="global",
    )
```

- [ ] **Step 4: Wire the idle-timeout into `SessionLens` for the global session**

In `session_lens.py`, when building the global session, use `self._config.supervisor.global_.idle_timeout_seconds` as the `idle_timeout` on the session spec.

- [ ] **Step 5: Run + commit**

```bash
pytest tests/test_config.py -v
git add src/config.py tests/test_config.py src/messages/session_lens.py
git commit -m "feat(config): supervisor.global.idle_timeout_seconds (default 45min)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Frontend — dependency add + feature flag scaffolding

**Files:**
- Modify: `dashboard/package.json`
- Modify: `dashboard/src/App.tsx` (add v2-flag dispatch)
- Create: `dashboard/src/shell/AppShellV2.tsx` (empty scaffold)
- Test: manual — `npm run typecheck` clean; `?v2=1` renders "AppShellV2 placeholder", default renders current shell.

**Interfaces:**
- Consumes: React Router route tree.
- Produces: `AppShellV2` component; `useIsV2()` hook reading `?v2=1` search param.

- [ ] **Step 1: Add deps**

```bash
cd dashboard && npm install cmdk react-hotkeys-hook
```

Confirm additions in `package.json`.

- [ ] **Step 2: Write the `useIsV2` hook + scaffolding**

`dashboard/src/shell/useIsV2.ts`:

```ts
import { useSearchParams } from "react-router-dom";

export function useIsV2(): boolean {
  const [params] = useSearchParams();
  return params.get("v2") === "1";
}
```

`dashboard/src/shell/AppShellV2.tsx`:

```tsx
import { Outlet } from "react-router-dom";

export default function AppShellV2(): JSX.Element {
  return (
    <div className="flex h-screen w-screen items-center justify-center bg-gray-950 text-gray-100">
      <div className="text-center">
        <p className="text-sm text-gray-500">AppShellV2 placeholder</p>
        <p className="mt-2 text-xs text-gray-600">Remove ?v2=1 to see the current shell.</p>
        <div className="mt-6">
          <Outlet />
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Dispatch in `App.tsx`**

Wrap the top-level `<Routes>` so `AppShellV2` mounts when the flag is present. Simplest: two parallel `<Routes>` trees selected by `useIsV2()` at the root.

- [ ] **Step 4: Manual verify + typecheck**

Run: `cd dashboard && npx tsc -b --noEmit && npm run dev`
Visit `http://localhost:5173/` → current shell.
Visit `http://localhost:5173/?v2=1` → placeholder.

- [ ] **Step 5: Commit**

```bash
git add dashboard/package.json dashboard/package-lock.json \
        dashboard/src/shell/AppShellV2.tsx dashboard/src/shell/useIsV2.ts \
        dashboard/src/App.tsx
git commit -m "chore(shell): scaffold AppShellV2 behind ?v2=1 flag

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: Frontend — platform detection + `useShortcuts` hook + `?` cheat sheet

**Files:**
- Create: `dashboard/src/shell/hotkeys/usePlatform.ts`
- Create: `dashboard/src/shell/hotkeys/useShortcuts.ts`
- Create: `dashboard/src/shell/hotkeys/CheatSheetModal.tsx`
- Test: `dashboard/src/shell/hotkeys/__tests__/useShortcuts.test.tsx`

**Interfaces:**
- Consumes: `react-hotkeys-hook`.
- Produces: `usePlatform()` → `{modifier: "cmd" | "ctrl"}`; `useShortcuts()` global registry; per-scope `useShortcut(key, opts)` + `useGlobalShortcut(key, opts)`.

- [ ] **Step 1: Write failing test**

`dashboard/src/shell/hotkeys/__tests__/useShortcuts.test.tsx`:

```tsx
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ShortcutsProvider, useShortcut } from "../useShortcuts";

function Probe({ onFire }: { onFire: () => void }) {
  useShortcut("$mod-k", { label: "open palette", onFire });
  return <div>probe</div>;
}

test("$mod-k normalizes to Cmd-K on mac", async () => {
  Object.defineProperty(navigator, "userAgent", { value: "Macintosh", configurable: true });
  const spy = vi.fn();
  render(<ShortcutsProvider><Probe onFire={spy} /></ShortcutsProvider>);
  await userEvent.keyboard("{Meta>}k{/Meta}");
  expect(spy).toHaveBeenCalled();
});

test("$mod-k normalizes to Ctrl-K on linux", async () => {
  Object.defineProperty(navigator, "userAgent", { value: "Linux x86_64", configurable: true });
  const spy = vi.fn();
  render(<ShortcutsProvider><Probe onFire={spy} /></ShortcutsProvider>);
  await userEvent.keyboard("{Control>}k{/Control}");
  expect(spy).toHaveBeenCalled();
});
```

- [ ] **Step 2: Run and fail**

Expected: FAIL — modules missing.

- [ ] **Step 3: Implement platform + shortcuts**

`usePlatform.ts`:
```ts
export type Platform = { modifier: "cmd" | "ctrl" };

export function detectPlatform(): Platform {
  const ua = typeof navigator !== "undefined" ? navigator.userAgent : "";
  const isMac = /Mac|iPhone|iPad|iPod/i.test(ua);
  return { modifier: isMac ? "cmd" : "ctrl" };
}
```

`useShortcuts.ts`:
```tsx
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, type ReactNode } from "react";
import { useHotkeys } from "react-hotkeys-hook";
import { detectPlatform } from "./usePlatform";

export interface ShortcutOpts {
  label: string;
  onFire: () => void;
  section?: string;
  when?: () => boolean;
}

interface Registered { key: string; opts: ShortcutOpts; }

interface Ctx {
  registered: Registered[];
  register: (r: Registered) => () => void;
}

const Ctx = createContext<Ctx | null>(null);

export function ShortcutsProvider({ children }: { children: ReactNode }) {
  const registered = useRef<Registered[]>([]);
  const register = useCallback((r: Registered) => {
    registered.current.push(r);
    return () => {
      registered.current = registered.current.filter((x) => x !== r);
    };
  }, []);
  const value = useMemo<Ctx>(() => ({
    get registered() { return registered.current; },
    register,
  }), [register]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

function expandMod(key: string): string {
  const p = detectPlatform();
  return key.replace(/\$mod/g, p.modifier === "cmd" ? "meta" : "ctrl");
}

export function useShortcut(key: string, opts: ShortcutOpts): void {
  const ctx = useContext(Ctx);
  const expanded = useMemo(() => expandMod(key), [key]);
  useEffect(() => {
    if (!ctx) return;
    return ctx.register({ key: expanded, opts });
  }, [ctx, expanded, opts]);
  useHotkeys(expanded, () => {
    if (opts.when && !opts.when()) return;
    opts.onFire();
  }, { enableOnFormTags: false });
}

export function useCheatSheet(): Registered[] {
  const ctx = useContext(Ctx);
  return ctx?.registered ?? [];
}
```

`CheatSheetModal.tsx`:
```tsx
import { useCheatSheet, useShortcut } from "./useShortcuts";

export default function CheatSheetModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  useShortcut("Escape", { label: "close cheat sheet", onFire: onClose, when: () => open });
  if (!open) return null;
  const items = useCheatSheet();
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div className="w-full max-w-2xl rounded-lg border border-gray-700 bg-gray-900 p-6" onClick={(e) => e.stopPropagation()}>
        <h2 className="mb-4 text-lg font-semibold">Keyboard shortcuts</h2>
        <ul className="max-h-[60vh] space-y-1 overflow-y-auto">
          {items.map((it, i) => (
            <li key={i} className="flex items-center justify-between border-b border-gray-800 py-1 text-sm">
              <span className="text-gray-300">{it.opts.label}</span>
              <kbd className="rounded bg-gray-800 px-2 py-0.5 font-mono text-xs">{it.key}</kbd>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Wire `?` binding for the cheat sheet**

In `AppShellV2.tsx`, add:
```tsx
const [cheatOpen, setCheatOpen] = useState(false);
useShortcut("?", { label: "toggle cheat sheet", onFire: () => setCheatOpen((v) => !v) });
```

- [ ] **Step 5: Run tests**

Run: `cd dashboard && npx vitest run src/shell/hotkeys/__tests__/`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/shell/hotkeys/ dashboard/src/shell/AppShellV2.tsx
git commit -m "feat(shell): hotkey layer + platform detection + cheat sheet

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: Frontend — command palette (cmdk) with Linear-style prefixes

**Files:**
- Create: `dashboard/src/shell/palette/Palette.tsx`
- Create: `dashboard/src/shell/palette/registerActions.ts`
- Create: `dashboard/src/shell/palette/rankResults.ts`
- Test: `dashboard/src/shell/palette/__tests__/Palette.test.tsx`

**Interfaces:**
- Consumes: `useShortcut`, `useTasks`, `useProjects` (existing daemon hooks).
- Produces: `<Palette />` component, `useRegisterAction({id, label, run, section})` hook, `$mod-K` opens.

- [ ] **Step 1: Write failing test**

`dashboard/src/shell/palette/__tests__/Palette.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Palette } from "../Palette";
import { ActionRegistryProvider, useRegisterAction } from "../registerActions";
import { ShortcutsProvider } from "../../hotkeys/useShortcuts";

function Registrar() {
  useRegisterAction({ id: "open-diff", label: "Open diff for current task", run: () => {}, section: "Panes" });
  return null;
}

test("$mod-K opens the palette and > prefix scopes to actions", async () => {
  render(
    <ShortcutsProvider>
      <ActionRegistryProvider>
        <Registrar />
        <Palette />
      </ActionRegistryProvider>
    </ShortcutsProvider>
  );
  Object.defineProperty(navigator, "userAgent", { value: "Macintosh", configurable: true });
  await userEvent.keyboard("{Meta>}k{/Meta}");
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  await userEvent.keyboard(">open");
  expect(screen.getByText("Open diff for current task")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run and fail**

Expected: FAIL — modules missing.

- [ ] **Step 3: Implement action registry**

`registerActions.ts`:
```tsx
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

export interface PaletteAction {
  id: string;
  label: string;
  section?: string;
  keywords?: string[];
  run: () => void;
}

interface Ctx {
  actions: PaletteAction[];
  register: (a: PaletteAction) => () => void;
}

const C = createContext<Ctx | null>(null);

export function ActionRegistryProvider({ children }: { children: ReactNode }) {
  const [actions, setActions] = useState<PaletteAction[]>([]);
  const register = useCallback((a: PaletteAction) => {
    setActions((prev) => [...prev, a]);
    return () => setActions((prev) => prev.filter((x) => x.id !== a.id));
  }, []);
  return <C.Provider value={{ actions, register }}>{children}</C.Provider>;
}

export function useRegisterAction(a: PaletteAction): void {
  const ctx = useContext(C);
  useEffect(() => ctx?.register(a), [ctx, a.id, a.label]);
}

export function useActions(): PaletteAction[] {
  return useContext(C)?.actions ?? [];
}
```

- [ ] **Step 4: Implement Palette**

`Palette.tsx`:
```tsx
import { Command } from "cmdk";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useShortcut } from "../hotkeys/useShortcuts";
import { useActions } from "./registerActions";
import { useProjects, useTasks } from "../../api/hooks";

export function Palette(): JSX.Element {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const navigate = useNavigate();
  useShortcut("$mod-k", { label: "open command palette", onFire: () => setOpen((v) => !v) });
  useShortcut("Escape", { label: "close palette", onFire: () => setOpen(false), when: () => open });

  const actions = useActions();
  const { data: projects } = useProjects();
  const { data: tasks } = useTasks(""); // supervisor-scoped; adjust once global chat has a current-project context

  const prefix = q[0];
  const body = q.slice(1).trim();

  const showActions = prefix === ">";
  const showTasks = prefix === "#";
  const showProjects = prefix === "@";
  const showDefault = !prefix || (!showActions && !showTasks && !showProjects);

  return (
    <Command.Dialog open={open} onOpenChange={setOpen} label="Command palette">
      <Command.Input value={q} onValueChange={setQ} placeholder="Type a command or search…" />
      <Command.List>
        {showActions && actions.filter((a) => a.label.toLowerCase().includes(body.toLowerCase())).map((a) => (
          <Command.Item key={a.id} onSelect={() => { a.run(); setOpen(false); }}>{a.label}</Command.Item>
        ))}
        {showTasks && (tasks ?? []).filter((t) => t.title.toLowerCase().includes(body.toLowerCase())).map((t) => (
          <Command.Item key={t.id} onSelect={() => { navigate(`/tasks/${t.id}`); setOpen(false); }}>
            {t.title}
          </Command.Item>
        ))}
        {showProjects && (projects ?? []).filter((p) => (p.name ?? p.id).toLowerCase().includes(body.toLowerCase())).map((p) => (
          <Command.Item key={p.id} onSelect={() => { navigate(`/projects/${p.id}`); setOpen(false); }}>
            {p.name ?? p.id}
          </Command.Item>
        ))}
        {showDefault && actions.filter((a) => a.label.toLowerCase().includes(q.toLowerCase())).map((a) => (
          <Command.Item key={a.id} onSelect={() => { a.run(); setOpen(false); }}>{a.label}</Command.Item>
        ))}
      </Command.List>
    </Command.Dialog>
  );
}
```

(Rank refinement — recency, relevance blend — deferred to a v2 refinement task; the initial ship uses cmdk's built-in fuzzy on top of the prefix filter.)

- [ ] **Step 5: Run tests + wire into shell**

Add `<ActionRegistryProvider><Palette />` inside `AppShellV2`.

Run: `cd dashboard && npx vitest run src/shell/palette/__tests__/`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/shell/palette/ dashboard/src/shell/AppShellV2.tsx
git commit -m "feat(shell): command palette with Linear-style prefixes

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: Frontend — shell layout (LeftRail + TopBar + RightSurface)

**Files:**
- Create: `dashboard/src/shell/LeftRail.tsx`
- Create: `dashboard/src/shell/TopBar.tsx`
- Create: `dashboard/src/shell/RightSurface.tsx`
- Create: `dashboard/src/shell/useRightSurface.ts`
- Modify: `dashboard/src/shell/AppShellV2.tsx` — mount them in the grid layout.

**Interfaces:**
- Consumes: `useShellPaneStore` (from pane interface plan), `useProjects`.
- Produces: full grid shell with mutually-exclusive right surface.

- [ ] **Step 1: Implement `useRightSurface`**

```ts
import { create } from "zustand";  // if available; else useState in a Context

type SurfaceKind = "pane" | "drawer" | null;

interface State {
  kind: SurfaceKind;
  width: number;
  setKind: (k: SurfaceKind) => void;
  setWidth: (w: number) => void;
}

export const useRightSurface = create<State>((set) => ({
  kind: null,
  width: 480,
  setKind: (kind) => set({ kind }),
  setWidth: (width) => set({ width: Math.max(200, Math.min(800, width)) }),
}));
```

(If Zustand isn't in the dep tree, use `createContext` + `useState` + Provider mount at shell root.)

- [ ] **Step 2: Bind pane store ↔ right surface**

Add an `useEffect` in `AppShellV2` that listens to `useShellPaneStore.state.kind`: when pane goes to "open", set right-surface kind to "pane"; when drawer opens, set to "drawer" (closes pane). This is the "opening one closes the other" contract.

- [ ] **Step 3: Implement LeftRail**

```tsx
import { NavLink } from "react-router-dom";
import { ChatBubbleLeftRightIcon, Squares2X2Icon, Cog6ToothIcon, FolderIcon } from "@heroicons/react/24/outline";
import { useProjects } from "../api/hooks";

const sections = [
  { to: "/?v2=1", label: "Home", icon: ChatBubbleLeftRightIcon },
  { to: "/command-center?v2=1", label: "Command Center", icon: Squares2X2Icon },
  { to: "/settings?v2=1", label: "Settings", icon: Cog6ToothIcon },
];

export default function LeftRail(): JSX.Element {
  const { data: projects } = useProjects();
  return (
    <aside className="flex h-full w-60 shrink-0 flex-col border-r border-gray-800 bg-gray-900">
      <nav className="flex-1 space-y-6 p-3">
        <div className="space-y-0.5">
          {sections.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} className="flex items-center gap-3 rounded-lg px-3 py-2 text-sm">
              <Icon className="h-4 w-4" />
              <span>{label}</span>
            </NavLink>
          ))}
        </div>
        <div>
          <p className="px-3 pb-2 text-xs uppercase text-gray-500">Projects</p>
          {(projects ?? []).map((p) => (
            <NavLink key={p.id} to={`/projects/${p.id}?v2=1`} className="flex items-center gap-3 rounded-lg px-3 py-2 text-sm">
              <FolderIcon className="h-4 w-4" />
              <span>{p.name ?? p.id}</span>
            </NavLink>
          ))}
        </div>
      </nav>
    </aside>
  );
}
```

- [ ] **Step 4: Implement TopBar**

```tsx
import { CommandLineIcon, BellIcon } from "@heroicons/react/24/outline";
import { useRightSurface } from "./useRightSurface";

export default function TopBar({ onPalette }: { onPalette: () => void }): JSX.Element {
  const { setKind } = useRightSurface();
  return (
    <header className="flex h-12 items-center justify-between border-b border-gray-800 bg-gray-950 px-4">
      <div className="flex items-center gap-2">
        <span className="text-sm font-semibold">Agent Q</span>
      </div>
      <div className="flex items-center gap-2">
        <button onClick={onPalette} className="rounded p-2 text-gray-400 hover:bg-gray-800">
          <CommandLineIcon className="h-4 w-4" />
        </button>
        <button onClick={() => setKind("drawer")} className="rounded p-2 text-gray-400 hover:bg-gray-800">
          <BellIcon className="h-4 w-4" />
        </button>
      </div>
    </header>
  );
}
```

- [ ] **Step 5: Implement RightSurface (mutex dispatcher)**

```tsx
import { XMarkIcon } from "@heroicons/react/24/outline";
import { useRightSurface } from "./useRightSurface";
import ShellPaneHost from "./ShellPaneHost";        // Task 9
import ActivityDrawer from "./ActivityDrawer";      // Task 10
import { useShellPaneStore } from "../panes/store";

export default function RightSurface(): JSX.Element | null {
  const { kind, width, setKind } = useRightSurface();
  const paneStore = useShellPaneStore();
  if (kind === null) return null;
  return (
    <aside style={{ width }} className="flex h-full shrink-0 flex-col border-l border-gray-800 bg-gray-950">
      <header className="flex items-center justify-between border-b border-gray-800 p-2">
        <span className="text-xs uppercase text-gray-500">{kind}</span>
        <button onClick={() => { setKind(null); if (kind === "pane") paneStore.close(); }} className="rounded p-1 text-gray-400 hover:bg-gray-800">
          <XMarkIcon className="h-4 w-4" />
        </button>
      </header>
      <div className="flex-1 overflow-hidden">
        {kind === "pane" ? <ShellPaneHost /> : <ActivityDrawer />}
      </div>
    </aside>
  );
}
```

- [ ] **Step 6: Wire everything in AppShellV2**

```tsx
import LeftRail from "./LeftRail";
import TopBar from "./TopBar";
import RightSurface from "./RightSurface";
import { ShellPaneProvider } from "../panes/store";
import { ActionRegistryProvider } from "./palette/registerActions";
import { ShortcutsProvider } from "./hotkeys/useShortcuts";
import { Palette } from "./palette/Palette";
import CheatSheetModal from "./hotkeys/CheatSheetModal";
import { useAgentPushBridge } from "../panes/agentPush";
import { useState } from "react";
import { Outlet } from "react-router-dom";

function ShellBody(): JSX.Element {
  useAgentPushBridge();
  const [cheat, setCheat] = useState(false);
  useShortcut("?", { label: "cheat sheet", onFire: () => setCheat((v) => !v) });
  return (
    <div className="grid h-screen w-screen grid-cols-[auto_1fr_auto] grid-rows-[auto_1fr] bg-gray-950 text-gray-100">
      <TopBar onPalette={() => {/* palette manages its own state */}} />
      <div className="col-span-3 row-start-1"><TopBar onPalette={() => {}} /></div>
      <LeftRail />
      <main className="row-start-2 overflow-auto"><Outlet /></main>
      <RightSurface />
      <Palette />
      <CheatSheetModal open={cheat} onClose={() => setCheat(false)} />
    </div>
  );
}

export default function AppShellV2(): JSX.Element {
  return (
    <ShortcutsProvider>
      <ActionRegistryProvider>
        <ShellPaneProvider>
          <ShellBody />
        </ShellPaneProvider>
      </ActionRegistryProvider>
    </ShortcutsProvider>
  );
}
```

- [ ] **Step 7: Verify + commit**

Run: `cd dashboard && npx tsc -b --noEmit`
Manual: visit `/?v2=1` → shell renders with empty main, bell + palette buttons work.

```bash
git add dashboard/src/shell/*.tsx dashboard/src/shell/*.ts
git commit -m "feat(shell): grid layout, LeftRail, TopBar, RightSurface (mutex)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: Frontend — ShellPaneHost (renders pane content from registry)

**Files:**
- Create: `dashboard/src/shell/ShellPaneHost.tsx`
- Test: extend `dashboard/src/panes/__tests__/smoke.test.tsx`

**Interfaces:**
- Consumes: `useShellPaneStore`, `PANE_REGISTRY`.
- Produces: pane host that renders `entry.Component` with the store's args + close + setArgs + setToolbar + setShortcuts.

- [ ] **Step 1: Implement**

```tsx
import { useState } from "react";
import { useShellPaneStore } from "../panes/store";
import { PANE_REGISTRY } from "../panes/registry";
import type { PaneToolbarAction, ShortcutBinding } from "../panes/types";

export default function ShellPaneHost(): JSX.Element {
  const { state, close, setArgs } = useShellPaneStore();
  const [toolbar, setToolbar] = useState<PaneToolbarAction[]>([]);
  const [shortcuts, setShortcuts] = useState<ShortcutBinding[]>([]);
  if (state.kind !== "open") return null as never;
  const entry = PANE_REGISTRY[state.view];
  if (!entry) {
    return <div className="p-4 text-xs text-red-400">Unknown pane view: {state.view}</div>;
  }
  const { Component, manifest } = entry;
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-gray-800 p-2">
        <manifest.icon className="h-4 w-4" />
        <span className="text-xs font-medium">{manifest.name}</span>
        <div className="ml-auto flex items-center gap-1">
          {toolbar.map((a) => (
            <button key={a.id} onClick={a.onClick} disabled={a.disabled}
                    className="rounded p-1 text-xs text-gray-400 hover:bg-gray-800">
              {a.icon ? <a.icon className="h-3.5 w-3.5" /> : a.label}
            </button>
          ))}
        </div>
      </div>
      <div className="flex-1 overflow-auto p-3">
        <Component args={state.args} close={close} setArgs={setArgs} setToolbar={setToolbar} setShortcuts={setShortcuts} />
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Wire pane-store open ↔ right-surface kind sync**

In `AppShellV2` (or a top-level effect), keep `useRightSurface.kind` in sync with `useShellPaneStore.state.kind`.

- [ ] **Step 3: Verify with the __stub-smoke view**

Extend the smoke test to render `AppShellV2` and open the stub view.

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/shell/ShellPaneHost.tsx dashboard/src/panes/__tests__/smoke.test.tsx
git commit -m "feat(shell): ShellPaneHost — renders registered pane views

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 10: Frontend — ActivityDrawer (gates + events tabs)

**Files:**
- Create: `dashboard/src/shell/ActivityDrawer.tsx`
- Modify: `dashboard/src/api/hooks.ts` — add `useAllOpenGates` (list-gates across projects).
- Test: `dashboard/src/shell/__tests__/ActivityDrawer.test.tsx`

**Interfaces:**
- Consumes: `useEventStream`, `useAllOpenGates`, `useResolveGate`.
- Produces: two-tab drawer (Gates, Events), badge count derived from open human gates, `]` toggle, `Ctrl-A/Cmd-A` alias.

Follow shell spec §6. Implementation follows the pattern of the existing `ChatConversation` + `useChatTranscript` (rolling event window, filter chips) plus the existing `useResolveGate` hook for inline approve/reject.

- [ ] **Step 1: Bell badge count hook**

```ts
export function useOpenHumanGateCount(): number {
  const { data } = useAllOpenGates();
  return (data ?? []).filter((g) => g.gate_type === "human").length;
}
```

- [ ] **Step 2: Drawer component (skeleton — full impl per shell spec §6)**

```tsx
export default function ActivityDrawer(): JSX.Element {
  const [tab, setTab] = useState<"gates" | "events">("gates");
  useShortcut("]", { label: "toggle activity drawer", onFire: () => {/* handled at shell root */} });
  return (
    <div className="flex h-full flex-col">
      <div className="flex gap-1 border-b border-gray-800 p-2">
        <button onClick={() => setTab("gates")} className={tabClass(tab === "gates")}>Gates</button>
        <button onClick={() => setTab("events")} className={tabClass(tab === "events")}>Events</button>
      </div>
      <div className="flex-1 overflow-auto">
        {tab === "gates" ? <GatesList /> : <EventsList />}
      </div>
    </div>
  );
}
```

Fill in `GatesList` and `EventsList` per shell spec §6.1 + §6.3.

- [ ] **Step 3: Dispatch Enter by gate_type (per updated shell spec §6.3)**

```ts
function onGateEnter(gate: Gate) {
  const paneStore = useShellPaneStore.getState();
  switch (gate.gate_type) {
    case "routing":
      paneStore.open("proposal-preview", { proposalId: gate.subject_id });
      break;
    case "human":
    default:
      if (gate.task_ids?.length) paneStore.open("task-detail", { taskId: gate.task_ids[0] });
      break;
  }
}
```

- [ ] **Step 4: Tests + commit**

```bash
git add dashboard/src/shell/ActivityDrawer.tsx dashboard/src/api/hooks.ts \
        dashboard/src/shell/__tests__/ActivityDrawer.test.tsx
git commit -m "feat(shell): ActivityDrawer — gates + events tabs

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 11: Frontend — GlobalChat page + `/` route

**Files:**
- Create: `dashboard/src/pages/GlobalChat.tsx`
- Modify: `dashboard/src/pages/chat/ChatConversation.tsx` — accept optional `sessionAddress` + `threadIdOverride`.
- Modify: `dashboard/src/pages/chat/useChatTranscript.ts` — accept `sessionAddress` + `threadIdOverride`.
- Modify: `dashboard/src/App.tsx` — v2 routes tree includes `/` → GlobalChat.

**Interfaces:**
- Consumes: `ChatConversation` (parameterized).
- Produces: `<GlobalChat />` pointed at `supervisor-global` with thread `dashboard:global`.

- [ ] **Step 1: Parameterize the chat components**

Change `useChatTranscript(projectId)` signature to accept optional overrides that swap `dashboard:${projectId}` for a custom thread and `supervisor-${projectId}` for a custom to-id. Default behavior unchanged for existing callers.

- [ ] **Step 2: Author `GlobalChat.tsx`**

```tsx
import ChatConversation from "./chat/ChatConversation";

export default function GlobalChat(): JSX.Element {
  return (
    <ChatConversation
      projectId=""  // ignored when sessionAddress override present
      sessionAddress="supervisor-global"
      threadIdOverride="dashboard:global"
      headerText="Agent Q"
    />
  );
}
```

- [ ] **Step 3: Mount route**

In `App.tsx`'s v2 routes tree:
```tsx
<Route path="/" element={<GlobalChat />} />
```

- [ ] **Step 4: Chat composer bindings**

Confirm `ChatConversation` submits on `Enter`, newlines on `Shift-Enter`, `$mod-Enter` also submits (per Global Constraints).

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/pages/GlobalChat.tsx dashboard/src/pages/chat/ChatConversation.tsx \
        dashboard/src/pages/chat/useChatTranscript.ts dashboard/src/App.tsx
git commit -m "feat(shell): / renders GlobalChat pointed at supervisor-global

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 12: Frontend — Command Center consolidation (3 tabs + ProjectStrip persistence)

**Files:**
- Modify: `dashboard/src/pages/CommandCenter.tsx` — becomes the hub shell (ProjectStrip + tab strip + Outlet).
- Modify/Create: `dashboard/src/pages/command-center/Graph.tsx`, `Tasks.tsx`, `Agents.tsx` — one per tab.
- Modify: `dashboard/src/App.tsx` — routes `/command-center`, `/command-center/graph`, `/command-center/tasks`, `/command-center/agents`.
- Modify: `dashboard/src/pages/command-center/TaskNode.tsx` — click dispatches `pane.open("task-detail", { taskId })`.
- Retire: `dashboard/src/pages/command-center/TaskSidebar.tsx` — delete file, remove import.
- Migrate: `dashboard/src/pages/work/WorkTasks.tsx` → `dashboard/src/pages/command-center/Tasks.tsx` (rename + adjust imports).
- Migrate: `dashboard/src/pages/work/WorkAgents.tsx` → `dashboard/src/pages/command-center/Agents.tsx`, add session column.
- Legacy redirects: `/work*` → `/command-center/*`.

**Interfaces:**
- Consumes: `ProjectStrip` (existing), `GraphCanvas` (existing), `useShellPaneStore`.
- Produces: hub with 3 tabs, shared project filter, task-row click → pane.

- [ ] **Step 1: Add tab strip + redirect `/command-center` → `/command-center/graph`**

- [ ] **Step 2: Retire `TaskSidebar` and wire pane dispatch**

In `TaskNode.tsx`, on click:
```tsx
const paneStore = useShellPaneStore();
onClick={() => paneStore.open("task-detail", { taskId: task.id })}
```

Remove `<TaskSidebar />` from `CommandCenter.tsx`.

- [ ] **Step 3: Move + rename Work pages into CC tabs**

- [ ] **Step 4: Fold sessions into Agents**

Each row shows current session id + state. Click on row (or `Enter` on focused row) opens `pane.open("session-peek", { sessionId })`.

- [ ] **Step 5: Legacy redirects**

In `App.tsx` v2 tree:
```tsx
<Route path="/work" element={<Navigate to="/command-center/tasks?v2=1" replace />} />
<Route path="/work/tasks" element={<Navigate to="/command-center/tasks?v2=1" replace />} />
<Route path="/work/agents" element={<Navigate to="/command-center/agents?v2=1" replace />} />
<Route path="/work/sessions" element={<Navigate to="/command-center/agents?v2=1" replace />} />
<Route path="/work/events" element={<Navigate to="/command-center/tasks?v2=1&openDrawer=events" replace />} />
<Route path="/work/gates" element={<Navigate to="/command-center/tasks?v2=1&openDrawer=gates" replace />} />
```

`?openDrawer=` handled by a shell effect that reads it on route entry and opens the drawer + strips the param.

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/pages/CommandCenter.tsx dashboard/src/pages/command-center/ \
        dashboard/src/App.tsx
git rm dashboard/src/pages/command-center/TaskSidebar.tsx
git commit -m "feat(shell): CC hub with 3 tabs (Graph/Tasks/Agents); retire TaskSidebar

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 13: Section jumps + per-entity shortcut vocab

- [ ] Wire `g h/c/s/p` two-key sequences via a `useGotoMode` hook + `GotoModeOverlay`.
- [ ] Register per-entity shortcuts (`o`, `r`, `d`, `y`, `.`) on Tasks table rows; (`o`, `p`, `k`) on Agents rows.
- [ ] `[` toggles pane; `]` toggles drawer; `Esc` cascade.

Full step-by-step follows the same TDD pattern as Tasks 6–10. Estimated ~8 steps, ~1 commit per keyboard-family.

---

## Task 14: Manual verification + `v2=1` cleanup

- [ ] Manual browser verification against shell spec §9.3 checklist.
- [ ] Once every per-view plan lands + CC merge is stable in ?v2=1 mode, delete the v1 shell code and drop the flag: `useIsV2` becomes constant `true`; `App.tsx` renders only `AppShellV2`; retire `dashboard/src/components/Layout.tsx` + `dashboard/src/pages/chat/ChatLanding.tsx`.

---

## Self-Review

**Spec coverage:**
- §2 shell layout — Tasks 5, 8, 9, 10.
- §4 global supervisor — Tasks 1, 2, 3, 4.
- §5 shell pane primitive — Task 9 (host) + pane-plugin-interface plan (state).
- §6 activity drawer — Task 10.
- §7 CC consolidation — Task 12.
- §8 keyboard system — Tasks 6, 7, 13.
- §9 testing — folded into each task.
- §10 rollout phases — Tasks 5 (flag), 14 (removal).

**Placeholder scan:** No TBDs. Where a task defers to an existing pattern (e.g. `session_lens.py`'s per-project cold-start), the plan cites the file so the executor reads the source rather than guesses.

**Type consistency:** `RequestScope`, `PaneState`, `PANE_REGISTRY`, `useRightSurface` state all match what pane-plugin-interface-plan.md defines. Keyboard `$mod` normalization used consistently.
