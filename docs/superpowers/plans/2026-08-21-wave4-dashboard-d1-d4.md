# Wave 4 — Dashboard Lane A · D1 Sessions · D2 Task explain+graph · D3 Gates · D4 Supervisor chat

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship four dashboard pages so a human can drive the daemon and the new Wave-2/3 subsystems end-to-end: D1 Sessions (list, peek, nudge, attach, live transcript stream), D2 Task explain + dependency graph, D3 Gates inbox (list + resolve), D4 Supervisor chat (per-project chat with the `supervisor-<pid>` session). This is Wave 4 lane A only. D5 Worktrees, D6 Harness editor, D7 Doctor, D8 Costs are explicitly deferred to post-MVP.

**Architecture:** Backend work is small — the underlying `_cmd_*` handlers already exist in `src/commands/session_commands.py`, `src/commands/task_commands.py`, `src/commands/gate_commands.py`, `src/commands/message_commands.py`. Missing pieces are: (1) categorization of `session_*`, `gate_*`, `explain_task`, `project_ready` in `_TOOL_CATEGORIES` so `src/api/codegen.py:build_category_routers()` auto-generates `POST /api/{category}/{command-name}` routes; (2) per-command Pydantic response models registered in new modules `src/api/models/session.py`, `src/api/models/gate.py`, `src/api/models/message.py`, plus additions in `src/api/models/task.py` (explain + graph); (3) merging those into `get_all_response_models()`. Frontend work builds React pages under `dashboard/src/pages/{project,system}/`, hooks in `dashboard/src/api/hooks.ts`, a new SSE transcript hook, WS invalidation for `gate.*` / `message.*` / `session.*`, and `after_seq` resume on the events websocket (server already supports the query param at `src/api/websocket.py:83`). Chat page posts to the existing dedicated route `POST /api/sessions/{name}/message` (`src/api/messages.py`) — `message_send` stays in `API_EXCLUDED` (see `src/api/codegen.py:41`).

**Tech Stack:** Python 3.12 + FastAPI + Pydantic v2 + pytest-asyncio; TypeScript 5.7 + Vite 6 + React 19 + TanStack Query v5 + Tailwind v4 + React Router v7; `@aq/ts-client` generated via `npm run generate:ts-client` (needs the daemon running, or `--from-file` against cached `openapi.json`); icons from `@heroicons/react/24/outline`.

## Global Constraints

- All commands return `{"success": bool, ...}` dicts (`_cmd_message_*` returns bare dicts / `{"error": ...}` — the API layer surfaces the error via 422, so **do not** add a wrapping "success" key).
- Every new response model registered in a per-category `RESPONSE_MODELS` dict, aggregated in `src/api/models/__init__.py::get_all_response_models()` (append to the tuple at line 63). Every command surfaced through codegen must appear in `RESPONSE_MODELS` — Task 1 lands a guard test that fails when a categorized command has no model.
- `message_send` **stays in** `API_EXCLUDED` (`src/api/codegen.py:41`). The chat page posts to `POST /api/sessions/{name}/message` (name = `supervisor-<project_id>`) and polls / streams via WS `message.*` events. Do not remove `message_send` from `API_EXCLUDED` or add a duplicate route.
- Every event type emitted must already be registered in `src/event_schemas.py` — the events used here (`gate.created`, `gate.resolved`, `gate.expired`, `message.sent`, `message.delivered`, `message.replied`, `session.started`, `session.exited`, `session.adopted`, `task.blocked`, `task.unblocked`) are already registered (verified). Do not add new event types.
- Python: ruff line-length 100, py312. `pytest tests/ -n auto` must not add NEW failures (record the baseline count in the merge PR).
- TypeScript: `npm --prefix dashboard run typecheck` must pass. No new lint errors: `npm --prefix dashboard run lint`.
- Follow the existing `hooks.ts` invalidation pattern (`invalidateMcpViews` / `invalidateProfileViews` / `invalidateProjectQueries`).
- New pages live in `dashboard/src/pages/{project,system}/` beside existing ones. Do not restructure existing routes; add new `<Route>` entries in `dashboard/src/App.tsx` and tabs in `dashboard/src/pages/project/ProjectLayout.tsx`.
- Do not use icon libraries other than `@heroicons/react/24/outline` (or `/solid`). Do not use `fetch` directly — go through generated SDK functions or the SSE `EventSource` for the transcript stream.

---

## Task 1: Backend surface for sessions + gates + explain + guard test

**Files:**
- Modify: `src/tools/definitions.py` (append entries in `_TOOL_CATEGORIES`)
- Create: `src/api/models/session.py`
- Create: `src/api/models/gate.py`
- Create: `src/api/models/message.py`
- Modify: `src/api/models/task.py` (add explain + ready models)
- Modify: `src/api/models/__init__.py` (import + include new modules in the merge tuple)
- Create: `tests/test_response_model_registry.py`
- Modify: `packages/aq-ts-client/openapi.json` will regenerate — not committed by hand
- Regenerate: `packages/aq-ts-client/` via `npm run generate:ts-client` (documented; committed as-is)

**Interfaces:**

- Consumes:
  - Existing handlers with these exact return shapes (verified in code):
    - `_cmd_session_list` → `{"success": True, "sessions": [session_dict + idle_seconds, stalled], "count": int}` where `session_dict` = `{id, name, task_id, project_id, profile_id, harness, provider, lifecycle, state, work_dir, started_at, last_activity, restarts, quarantined_at, sleep_reason, epoch}`.
    - `_cmd_session_show` → `{"success": True, "session": session_dict}`.
    - `_cmd_session_peek` → `{"success": True, "session_id": str, "output": str, "note": str|absent}`.
    - `_cmd_session_attach` → `{"success": True, "session_id": str, "attach_command": str}`.
    - `_cmd_session_nudge` → `{"success": True|False, "session_id": str, "delivered": bool|absent, "error": str|absent}`.
    - `_cmd_session_logs` → `{"success": True, "session_id": str, "source": "transcript"|"peek", "entries": [{uuid, parent_uuid, type, text, model, usage, ts}]|absent, "output": str|absent}`.
    - `_cmd_session_kill` → `{"success": True, "session_id": str, ...}` (fenced kill — provider-dependent extras).
    - `_cmd_gate_list` → `{"success": True, "gates": [{id, gate_type, project_id, title, question, status, await_id, timeout_at, created_at, resolved_at, resolved_by, resolution}]}` (fields from `gate_queries.list_gates`).
    - `_cmd_gate_show` → `{"success": True, "gate": {...}, "waiters": [task_id, ...]}`.
    - `_cmd_gate_create` → `{"success": True, "gate_id": str, "gate": {...}}`.
    - `_cmd_gate_resolve` → `{"success": True, "gate_id": str, "unblocked_task_ids": [str, ...]}`.
    - `_cmd_explain_task` → `{"success": True, "reasons": [{code, detail, ref}]}` (`Reason` is a `TypedDict` from `src/explain.py`).
    - `_cmd_project_ready` → `{"success": True, "ready": [{task_id, title, priority}], "withheld": [{task_id, reasons: [Reason]}]}`.
    - `_cmd_task_deps` / `_cmd_get_task_dependencies` → `{"task_id, title, status, depends_on: [{id,title,status}], blocks: [{id,title,status}]}` (already registered as `TaskDepsResponse`).
    - `_cmd_message_list` → `{count: int, messages: [message_dict]}` (bare dict, no top-level success).
    - Existing endpoint `POST /api/sessions/{name}/message` — do not touch.

- Produces:
  - Category entries in `_TOOL_CATEGORIES` (append to `src/tools/definitions.py`):
    ```python
    # session — operator-visible session control (session-runtime spec)
    "session_list": "system",
    "session_show": "system",
    "session_peek": "system",
    "session_attach": "system",
    "session_nudge": "system",
    "session_logs": "system",
    "session_kill": "system",
    # explain + ready frontier (work-graph WG-4)
    "explain_task": "task",
    "project_ready": "task",
    ```
    (There is no "session" CategoryMeta in `src/tools/registry.py::CATEGORIES`; sessions belong under the operator-facing `system` category alongside `orchestrator_control` and `get_status`. Verified: `system` category exists at `src/tools/registry.py:96`. Adding a new category would break every existing CLI/MCP surface — keep sessions under `system`.)
  - Response model modules with `RESPONSE_MODELS` dicts; import + merge in `src/api/models/__init__.py`.

- [ ] **Step 1:** Create the failing guard test `tests/test_response_model_registry.py`:

```python
"""Guard: every categorized command surfaced through the codegen router has
a Pydantic response model registered.  Without this, the generated TS client
sees ``unknown`` for that call, which silently breaks the dashboard."""

from __future__ import annotations

import pytest

from src.api.codegen import API_EXCLUDED
from src.api.models import get_all_response_models
from src.tools import _CLI_CATEGORY_OVERRIDES, _TOOL_CATEGORIES

# Commands that intentionally return an unstructured dict (extra="allow") and
# are declared with model_config={"extra": "allow"} elsewhere.  Add here only
# with a code comment justifying why.
_UNSTRUCTURED_EXEMPT: set[str] = set()


@pytest.mark.parametrize(
    "cmd_name",
    sorted(
        {name for name, _cat in _TOOL_CATEGORIES.items()}
        | set(_CLI_CATEGORY_OVERRIDES)
        - API_EXCLUDED
        - _UNSTRUCTURED_EXEMPT
    ),
)
def test_every_categorized_command_has_response_model(cmd_name: str) -> None:
    models = get_all_response_models()
    assert cmd_name in models, (
        f"Command '{cmd_name}' is categorized (auto-generates a REST route) "
        "but has no entry in any src/api/models/*.py RESPONSE_MODELS dict. "
        "Add one so the generated TS client has a concrete type."
    )
```

- [ ] **Step 2:** Run `pytest tests/test_response_model_registry.py -v`. Confirm it fails with missing models for the commands currently uncovered — expected failing names include `message_send`, `message_reply`, `message_inbox`, `message_list`, plus (after Step 3) `session_*`, `gate_*`, `explain_task`, `project_ready`.

- [ ] **Step 3:** Append to `src/tools/definitions.py::_TOOL_CATEGORIES` (place beside the existing `# task` and `# system` blocks — do not reorder existing entries):

```python
    # session — operator surface (session-runtime spec §3, §5)
    "session_list": "system",
    "session_show": "system",
    "session_peek": "system",
    "session_attach": "system",
    "session_nudge": "system",
    "session_logs": "system",
    "session_kill": "system",
    # gate — work-graph WG-3 operator surface
    "gate_create": "task",
    "gate_list": "task",
    "gate_show": "task",
    "gate_resolve": "task",
    # explain + ready frontier — work-graph WG-4
    "explain_task": "task",
    "project_ready": "task",
```

- [ ] **Step 4:** Create `src/api/models/session.py`:

```python
"""Response models for session commands (session-runtime spec §3, §5)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class SessionSummary(BaseModel):
    """One row of ``session_list`` output.

    ``idle_seconds`` and ``stalled`` are derived per-row in
    ``_cmd_session_list``; every other field mirrors ``sessions`` table
    columns via ``SessionCommandsMixin._session_dict``.
    """

    id: str
    name: str
    task_id: str | None = None
    project_id: str | None = None
    profile_id: str | None = None
    harness: str | None = None
    provider: str | None = None
    lifecycle: str | None = None
    state: str | None = None
    work_dir: str | None = None
    started_at: float | None = None
    last_activity: float | None = None
    restarts: int = 0
    quarantined_at: float | None = None
    sleep_reason: str | None = None
    epoch: int | None = None
    idle_seconds: float = 0.0
    stalled: bool = False


class ListSessionsResponse(BaseModel):
    success: bool = True
    sessions: list[SessionSummary] = []
    count: int = 0


class ShowSessionResponse(BaseModel):
    success: bool = True
    session: SessionSummary


class SessionPeekResponse(BaseModel):
    success: bool = True
    session_id: str
    output: str = ""
    note: str | None = None


class SessionAttachResponse(BaseModel):
    success: bool = True
    session_id: str
    attach_command: str


class SessionNudgeResponse(BaseModel):
    success: bool
    session_id: str
    delivered: bool = False
    error: str | None = None


class TranscriptEntryModel(BaseModel):
    uuid: str
    parent_uuid: str | None = None
    type: str
    text: str = ""
    model: str | None = None
    usage: dict[str, Any] | None = None
    ts: float = 0.0


class SessionLogsResponse(BaseModel):
    """Union: transcript entries OR peek-fallback string output.

    ``source`` discriminates; extra keys allowed so a peek-fallback row
    that echoes ``note`` from ``_cmd_session_peek`` still validates.
    """

    model_config = {"extra": "allow"}
    success: bool = True
    session_id: str
    source: str = "transcript"
    entries: list[TranscriptEntryModel] | None = None
    output: str | None = None


class SessionKillResponse(BaseModel):
    model_config = {"extra": "allow"}
    success: bool = True
    session_id: str


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "session_list": ListSessionsResponse,
    "session_show": ShowSessionResponse,
    "session_peek": SessionPeekResponse,
    "session_attach": SessionAttachResponse,
    "session_nudge": SessionNudgeResponse,
    "session_logs": SessionLogsResponse,
    "session_kill": SessionKillResponse,
}
```

- [ ] **Step 5:** Create `src/api/models/gate.py`:

```python
"""Response models for gate commands (work-graph WG-3)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class GateSummary(BaseModel):
    """Gate row as returned by ``gate_list`` / ``gate_show``.

    Fields come from ``src/database/queries/gate_queries.py`` which returns
    dict rows including the resolved metadata columns.
    """

    model_config = {"extra": "allow"}
    id: str
    gate_type: str
    project_id: str
    title: str
    question: str = ""
    status: str = "open"
    await_id: str | None = None
    timeout_at: float | None = None
    created_at: float | None = None
    resolved_at: float | None = None
    resolved_by: str | None = None
    resolution: str | None = None


class GateCreatePayload(BaseModel):
    """Echoed by ``gate_create`` — matches the ``gate.created`` event payload."""

    model_config = {"extra": "allow"}
    gate_id: str
    gate_type: str
    project_id: str
    title: str
    question: str = ""
    await_id: str | None = None
    timeout_at: float | None = None
    waiter_task_ids: list[str] = []


class GateCreateResponse(BaseModel):
    success: bool = True
    gate_id: str
    gate: GateCreatePayload


class GateListResponse(BaseModel):
    success: bool = True
    gates: list[GateSummary] = []


class GateShowResponse(BaseModel):
    success: bool = True
    gate: GateSummary
    waiters: list[str] = []


class GateResolveResponse(BaseModel):
    success: bool = True
    gate_id: str
    unblocked_task_ids: list[str] = []


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "gate_create": GateCreateResponse,
    "gate_list": GateListResponse,
    "gate_show": GateShowResponse,
    "gate_resolve": GateResolveResponse,
}
```

- [ ] **Step 6:** Create `src/api/models/message.py`:

```python
"""Response models for message commands (supervisor-agent §6.1).

Note: ``_cmd_message_*`` returns bare dicts (no top-level ``success`` key);
the API layer converts ``{"error": ...}`` responses to HTTP 422.  The models
below reflect the success-branch dict shape and mark unusual/optional fields
so the generated TS client sees stable types.  ``message_send`` remains in
API_EXCLUDED and is *not* modeled here — the chat page uses the dedicated
``POST /api/sessions/{name}/message`` endpoint.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class MessageModel(BaseModel):
    """Rendered message dict (see ``src/commands/message_commands.py::message_to_dict``)."""

    model_config = {"extra": "allow"}
    id: str
    project_id: str | None = None
    from_kind: str
    from_id: str
    from_: str | None = None  # rendered "kind:id"; keyed as "from" in JSON
    to_kind: str
    to_id: str
    to: str | None = None
    thread_id: str | None = None
    subject: str | None = None
    body: str
    priority: int = 100
    created_at: float | None = None
    delivered_at: float | None = None
    read_at: float | None = None
    read: bool = False
    delivered: bool = False
    archive_after_inject: bool = False
    archived_at: float | None = None
    reply_to_id: str | None = None
    via: str | None = None


class MessageReplyResponse(BaseModel):
    model_config = {"extra": "allow"}
    message_id: str
    reply_id: str
    reply: MessageModel


class MessageInboxResponse(BaseModel):
    model_config = {"extra": "allow"}
    to_kind: str
    to_id: str
    count: int = 0
    injected: int | None = None
    archived: int | None = None
    messages: list[MessageModel] = []


class MessageListResponse(BaseModel):
    count: int = 0
    messages: list[MessageModel] = []


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    # message_send is API_EXCLUDED — see module docstring.
    "message_reply": MessageReplyResponse,
    "message_inbox": MessageInboxResponse,
    "message_list": MessageListResponse,
}
```

- [ ] **Step 7:** Extend `src/api/models/task.py` — append these classes above the `RESPONSE_MODELS` dict:

```python
class ExplainReason(BaseModel):
    code: str
    detail: str = ""
    ref: str | None = None


class ExplainTaskResponse(BaseModel):
    success: bool = True
    reasons: list[ExplainReason] = []


class ReadyTask(BaseModel):
    task_id: str
    title: str
    priority: int = 0


class WithheldTask(BaseModel):
    task_id: str
    reasons: list[ExplainReason] = []


class ProjectReadyResponse(BaseModel):
    success: bool = True
    ready: list[ReadyTask] = []
    withheld: list[WithheldTask] = []
```

Then add to the `RESPONSE_MODELS` dict at the bottom of that file:

```python
    "explain_task": ExplainTaskResponse,
    "project_ready": ProjectReadyResponse,
```

- [ ] **Step 8:** Update `src/api/models/__init__.py::get_all_response_models` — extend the import list and the merge tuple:

```python
def get_all_response_models() -> dict[str, type[BaseModel]]:
    """Collect RESPONSE_MODELS from every category module."""
    from src.api.models import (
        agent,
        files,
        gate,
        git,
        mcp,
        memory,
        message,
        playbook,
        plugin,
        project,
        session,
        system,
        task,
    )

    merged: dict[str, type[BaseModel]] = {}
    for mod in (
        task, project, agent, git, memory, files, system,
        plugin, mcp, playbook, session, gate, message,
    ):
        merged.update(mod.RESPONSE_MODELS)
    return merged
```

- [ ] **Step 9:** Re-run `pytest tests/test_response_model_registry.py -v` — every parametrized case passes.

- [ ] **Step 10:** Regenerate the TS client. Start the daemon in one terminal (`./run.sh start`), then in another:

```bash
cd /home/jkern/dev/agent-queue2
npm run generate:ts-client
```

If the daemon isn't handy: capture `openapi.json` once (`curl http://127.0.0.1:8081/openapi.json -o openapi.json`) and use `npm run generate:ts-client -- --from-file`. Verify `packages/aq-ts-client/dist/` now exports typed clients for `sessionList`, `sessionPeek`, `sessionAttach`, `sessionNudge`, `sessionLogs`, `sessionKill`, `sessionShow`, `explainTask`, `projectReady`, `gateList`, `gateShow`, `gateCreate`, `gateResolve` (name mapping: `POST /api/system/session-list` → `sessionList`, etc.).

- [ ] **Step 11:** Full suite regression: `pytest tests/ -n auto`. Zero NEW failures vs. the pre-plan baseline.

- [ ] **Step 12:** Commit: `feat(api): response models + categorization for session/gate/explain (D1-D3 backend)`

---

## Task 2: `useTranscriptStream` SSE hook + Sessions pages (D1)

**Files:**
- Create: `dashboard/src/ws/useTranscriptStream.ts`
- Create: `dashboard/src/pages/system/Sessions.tsx`
- Create: `dashboard/src/pages/project/Sessions.tsx`
- Create: `dashboard/src/pages/SessionDetail.tsx` (shared detail view with the transcript stream + peek + nudge form + attach command)
- Modify: `dashboard/src/api/hooks.ts` (new hooks)
- Modify: `dashboard/src/App.tsx` (new routes)
- Modify: `dashboard/src/pages/project/ProjectLayout.tsx` (new tab)
- Modify: `dashboard/src/components/Layout.tsx` (add "Sessions" link in the system section; verify the existing pattern by reading the file first)

**Interfaces:**

- Consumes: generated SDK from Task 1 (`sessionList`, `sessionShow`, `sessionPeek`, `sessionNudge`, `sessionAttach`, `sessionLogs`, `sessionKill`); native `EventSource` for SSE against `/api/sessions/{id}/stream` (frame shape from `src/api/sessions.py` verified: `{source, uuid, parent_uuid, type, text, model, usage, ts}` or `{source:"peek", text, ts}`).
- Produces: `useSessions(projectId?)`, `useSession(sessionId)`, `useSessionPeek(sessionId)`, `useSessionNudge()`, `useSessionAttach()`, `useSessionKill()`, plus `useTranscriptStream(sessionId, opts)` returning `{entries, status, error, clear}`.

- [ ] **Step 1:** Read `dashboard/src/components/Layout.tsx` to learn the nav pattern before editing it. (No code snippet here because the exact file needs verification — copy the existing "Events" or "Playbooks" system link, add one for "Sessions" pointing to `/system/sessions`.)

- [ ] **Step 2:** Add hooks in `dashboard/src/api/hooks.ts`. First extend the SDK imports at the top of the file:

```typescript
import {
  // ... existing imports ...
  sessionList,
  sessionShow,
  sessionPeek,
  sessionNudge,
  sessionAttach,
  sessionLogs,
  sessionKill,
} from "./client";
import type {
  // ... existing ...
  ListSessionsResponse,
  ShowSessionResponse,
  SessionPeekResponse,
  SessionNudgeResponse,
  SessionAttachResponse,
  SessionLogsResponse,
  SessionSummary,
} from "./client";
```

Add these hook definitions at the bottom of `hooks.ts` (place before the final `// --- System config ---` section):

```typescript
// --- Sessions (session-runtime spec) ---

export type { SessionSummary, ListSessionsResponse, SessionLogsResponse };

export function useSessions(projectId?: string) {
  return useQuery({
    queryKey: ["sessions", projectId ?? "all"],
    queryFn: async () => {
      const body: Record<string, unknown> = {};
      if (projectId) body.project_id = projectId;
      const { data } = await sessionList({ body, throwOnError: true });
      return ((data as ListSessionsResponse).sessions ?? []) as SessionSummary[];
    },
    refetchInterval: 15_000,
  });
}

export function useSession(sessionId: string) {
  return useQuery({
    queryKey: ["session", sessionId],
    queryFn: async () => {
      const { data } = await sessionShow({
        body: { session_id: sessionId },
        throwOnError: true,
      });
      return (data as ShowSessionResponse).session;
    },
    enabled: !!sessionId,
    refetchInterval: 15_000,
  });
}

export function useSessionPeek(sessionId: string, lines = 120) {
  return useQuery({
    queryKey: ["session-peek", sessionId, lines],
    queryFn: async () => {
      const { data } = await sessionPeek({
        body: { session_id: sessionId, lines },
        throwOnError: true,
      });
      return data as SessionPeekResponse;
    },
    enabled: !!sessionId,
    refetchInterval: 10_000,
  });
}

export function useSessionAttach(sessionId: string) {
  return useQuery({
    queryKey: ["session-attach", sessionId],
    queryFn: async () => {
      const { data } = await sessionAttach({
        body: { session_id: sessionId },
        throwOnError: true,
      });
      return data as SessionAttachResponse;
    },
    enabled: !!sessionId,
    staleTime: 60_000,
  });
}

function invalidateSessionViews(
  queryClient: ReturnType<typeof useQueryClient>,
  sessionId?: string,
) {
  queryClient.invalidateQueries({ queryKey: ["sessions"] });
  if (sessionId) {
    queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
    queryClient.invalidateQueries({ queryKey: ["session-peek", sessionId] });
  }
}

export function useSessionNudge() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { session_id: string; text: string }) =>
      (await sessionNudge({ body: input, throwOnError: true })).data as SessionNudgeResponse,
    onSuccess: (_d, variables) => invalidateSessionViews(queryClient, variables.session_id),
  });
}

export function useSessionKill() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { session_id: string }) =>
      (await sessionKill({ body: input, throwOnError: true })).data,
    onSuccess: (_d, variables) => invalidateSessionViews(queryClient, variables.session_id),
  });
}

export function useSessionLogs(sessionId: string, limit = 200) {
  return useQuery({
    queryKey: ["session-logs", sessionId, limit],
    queryFn: async () => {
      const { data } = await sessionLogs({
        body: { session_id: sessionId, limit },
        throwOnError: true,
      });
      return data as SessionLogsResponse;
    },
    enabled: !!sessionId,
  });
}
```

- [ ] **Step 3:** Create `dashboard/src/ws/useTranscriptStream.ts`:

```typescript
/**
 * SSE hook wrapping `GET /api/sessions/{session_id}/stream`.
 *
 * The server (src/api/sessions.py) sends `data:` frames in two shapes:
 *   - transcript: {source:"transcript", uuid, parent_uuid, type, text, model, usage, ts}
 *   - peek:       {source:"peek", text, ts}
 * Plus periodic ": heartbeat" comments which EventSource discards.
 *
 * The hook keeps entries in a bounded buffer (default 2000) so long-running
 * sessions don't grow unboundedly. Reconnect is handled by EventSource
 * natively; we surface the `readyState` as ConnectionStatus for the UI.
 */

import { useEffect, useRef, useState, useCallback } from "react";

export type TranscriptSource = "transcript" | "peek";

export interface TranscriptFrame {
  source: TranscriptSource;
  uuid?: string;
  parent_uuid?: string | null;
  type?: string;
  text: string;
  model?: string | null;
  usage?: unknown;
  ts: number;
  // Locally assigned monotonic index so React keys stay stable when a frame
  // has no uuid (peek fallback).
  _idx: number;
}

export type StreamStatus = "connecting" | "open" | "closed" | "error";

interface UseTranscriptStreamOptions {
  bufferSize?: number;
  enabled?: boolean;
}

const DEFAULT_BUFFER = 2000;

export function useTranscriptStream(
  sessionId: string | null | undefined,
  opts: UseTranscriptStreamOptions = {},
) {
  const { bufferSize = DEFAULT_BUFFER, enabled = true } = opts;
  const [entries, setEntries] = useState<TranscriptFrame[]>([]);
  const [status, setStatus] = useState<StreamStatus>("closed");
  const [error, setError] = useState<string | null>(null);
  const idxRef = useRef(0);
  const esRef = useRef<EventSource | null>(null);

  const clear = useCallback(() => {
    setEntries([]);
    idxRef.current = 0;
  }, []);

  useEffect(() => {
    if (!enabled || !sessionId) return;

    const base =
      import.meta.env.VITE_API_URL ||
      `${window.location.protocol}//${window.location.host}`;
    const url = `${base}/api/sessions/${encodeURIComponent(sessionId)}/stream`;

    setStatus("connecting");
    setError(null);
    const es = new EventSource(url);
    esRef.current = es;

    es.onopen = () => setStatus("open");

    es.onmessage = (msg) => {
      try {
        const raw = JSON.parse(msg.data) as Omit<TranscriptFrame, "_idx">;
        const frame: TranscriptFrame = { ...raw, _idx: idxRef.current++ };
        setEntries((prev) => {
          const next = prev.length >= bufferSize
            ? prev.slice(prev.length - bufferSize + 1)
            : prev.slice();
          next.push(frame);
          return next;
        });
      } catch {
        // Ignore malformed frame; server also emits comment heartbeats
        // which EventSource never surfaces to onmessage anyway.
      }
    };

    es.onerror = () => {
      setStatus("error");
      setError("stream error (EventSource will retry)");
    };

    return () => {
      es.close();
      esRef.current = null;
      setStatus("closed");
    };
  }, [sessionId, enabled, bufferSize]);

  return { entries, status, error, clear };
}
```

- [ ] **Step 4:** Create the shared detail view `dashboard/src/pages/SessionDetail.tsx`:

```tsx
import { useState } from "react";
import { useParams } from "react-router-dom";
import {
  PlayIcon,
  StopIcon,
  PaperAirplaneIcon,
  ArrowPathIcon,
  ClipboardIcon,
} from "@heroicons/react/24/outline";
import {
  useSession,
  useSessionAttach,
  useSessionNudge,
  useSessionKill,
} from "../api/hooks";
import { useTranscriptStream } from "../ws/useTranscriptStream";

export default function SessionDetail() {
  const { sessionId = "" } = useParams();
  const { data: session, isLoading } = useSession(sessionId);
  const attach = useSessionAttach(sessionId);
  const nudge = useSessionNudge();
  const kill = useSessionKill();
  const [text, setText] = useState("");
  const [streamOn, setStreamOn] = useState(true);
  const { entries, status, error, clear } = useTranscriptStream(sessionId, {
    enabled: streamOn,
  });

  if (isLoading) return <div className="p-6 text-gray-400">Loading…</div>;
  if (!session) return <div className="p-6 text-gray-400">Session not found</div>;

  return (
    <div className="space-y-4">
      <header className="space-y-1">
        <p className="text-xs uppercase tracking-wider text-gray-500">Session</p>
        <h1 className="text-2xl font-bold">{session.name}</h1>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-500">
          <span>id: <span className="font-mono">{session.id}</span></span>
          <span>task: {session.task_id ?? "—"}</span>
          <span>project: {session.project_id ?? "—"}</span>
          <span>harness: {session.harness ?? "—"}</span>
          <span>provider: {session.provider ?? "—"}</span>
          <span>lifecycle: {session.lifecycle ?? "—"}</span>
          <span>state: {session.state ?? "—"}</span>
          <span>idle: {Math.round(session.idle_seconds ?? 0)}s</span>
          {session.stalled && (
            <span className="rounded bg-amber-500/10 px-1 text-amber-400">STALLED</span>
          )}
        </div>
      </header>

      <section className="grid gap-4 md:grid-cols-2">
        <div className="rounded border border-gray-800 bg-gray-950 p-3">
          <h2 className="mb-2 text-sm font-semibold text-gray-300">Attach</h2>
          {attach.data ? (
            <div className="flex items-center gap-2">
              <code className="flex-1 overflow-x-auto rounded bg-black/40 px-2 py-1 font-mono text-xs">
                {attach.data.attach_command}
              </code>
              <button
                className="rounded p-1 text-gray-400 hover:text-gray-200"
                aria-label="Copy attach command"
                onClick={() =>
                  navigator.clipboard.writeText(attach.data!.attach_command)
                }
              >
                <ClipboardIcon className="h-4 w-4" />
              </button>
            </div>
          ) : (
            <p className="text-xs text-gray-500">No attach command available.</p>
          )}
        </div>
        <div className="rounded border border-gray-800 bg-gray-950 p-3">
          <h2 className="mb-2 text-sm font-semibold text-gray-300">Nudge</h2>
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (!text.trim()) return;
              nudge.mutate(
                { session_id: sessionId, text },
                { onSuccess: () => setText("") },
              );
            }}
          >
            <input
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Message the agent…"
              className="flex-1 rounded border border-gray-800 bg-gray-900 px-2 py-1 text-sm text-gray-200"
            />
            <button
              type="submit"
              disabled={nudge.isPending || !text.trim()}
              className="inline-flex items-center gap-1 rounded bg-indigo-600 px-3 py-1 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              <PaperAirplaneIcon className="h-4 w-4" />
              Send
            </button>
          </form>
          {nudge.error && (
            <p className="mt-2 text-xs text-red-400">
              {(nudge.error as Error).message}
            </p>
          )}
        </div>
      </section>

      <section className="rounded border border-gray-800 bg-gray-950">
        <div className="flex items-center justify-between border-b border-gray-800 px-3 py-2">
          <h2 className="text-sm font-semibold text-gray-300">
            Transcript stream
            <span className="ml-2 text-xs text-gray-500">({status})</span>
          </h2>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setStreamOn((v) => !v)}
              className="inline-flex items-center gap-1 rounded border border-gray-800 px-2 py-1 text-xs text-gray-300 hover:bg-gray-900"
            >
              {streamOn ? <StopIcon className="h-3 w-3" /> : <PlayIcon className="h-3 w-3" />}
              {streamOn ? "Pause" : "Resume"}
            </button>
            <button
              onClick={clear}
              className="inline-flex items-center gap-1 rounded border border-gray-800 px-2 py-1 text-xs text-gray-300 hover:bg-gray-900"
            >
              <ArrowPathIcon className="h-3 w-3" />
              Clear
            </button>
            <button
              onClick={() => kill.mutate({ session_id: sessionId })}
              className="inline-flex items-center gap-1 rounded border border-red-900 px-2 py-1 text-xs text-red-400 hover:bg-red-950"
            >
              Kill
            </button>
          </div>
        </div>
        {error && <p className="px-3 py-1 text-xs text-amber-400">{error}</p>}
        <div className="max-h-[60vh] overflow-y-auto p-3 font-mono text-xs">
          {entries.length === 0 ? (
            <p className="text-gray-500">Waiting for output…</p>
          ) : (
            entries.map((e) => (
              <div key={e._idx} className="mb-2 whitespace-pre-wrap">
                <span className="mr-2 text-gray-600">
                  {e.source === "peek" ? "[peek]" : `[${e.type ?? "?"}]`}
                </span>
                <span className="text-gray-200">{e.text}</span>
              </div>
            ))
          )}
        </div>
      </section>
    </div>
  );
}
```

- [ ] **Step 5:** Create `dashboard/src/pages/system/Sessions.tsx`:

```tsx
import { Link } from "react-router-dom";
import { useSessions } from "../../api/hooks";

export default function SystemSessions() {
  const { data: sessions = [], isLoading, error } = useSessions();

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-bold">Sessions</h1>
        <p className="text-sm text-gray-500">
          Every running or recent agent session across all projects.
        </p>
      </header>

      {isLoading && <p className="text-sm text-gray-400">Loading…</p>}
      {error && (
        <p className="text-sm text-red-400">
          Failed to load sessions: {(error as Error).message}
        </p>
      )}

      <div className="overflow-hidden rounded border border-gray-800">
        <table className="w-full text-sm">
          <thead className="bg-gray-900 text-left text-xs uppercase tracking-wider text-gray-500">
            <tr>
              <th className="px-3 py-2">Name</th>
              <th className="px-3 py-2">Project</th>
              <th className="px-3 py-2">Task</th>
              <th className="px-3 py-2">Harness</th>
              <th className="px-3 py-2">State</th>
              <th className="px-3 py-2">Idle</th>
              <th className="px-3 py-2">Restarts</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800">
            {sessions.map((s) => (
              <tr key={s.id} className="hover:bg-gray-900">
                <td className="px-3 py-2">
                  <Link
                    to={`/sessions/${s.id}`}
                    className="text-indigo-400 hover:text-indigo-300"
                  >
                    {s.name}
                  </Link>
                </td>
                <td className="px-3 py-2 text-gray-400">{s.project_id ?? "—"}</td>
                <td className="px-3 py-2 text-gray-400">{s.task_id ?? "—"}</td>
                <td className="px-3 py-2 text-gray-400">{s.harness ?? "—"}</td>
                <td className="px-3 py-2">
                  <span
                    className={`rounded px-2 py-0.5 text-xs ${
                      s.stalled ? "bg-amber-500/10 text-amber-400" : "bg-gray-800 text-gray-300"
                    }`}
                  >
                    {s.state ?? "?"}
                  </span>
                </td>
                <td className="px-3 py-2 text-gray-400">
                  {Math.round(s.idle_seconds ?? 0)}s
                </td>
                <td className="px-3 py-2 text-gray-400">{s.restarts ?? 0}</td>
              </tr>
            ))}
            {sessions.length === 0 && !isLoading && (
              <tr>
                <td colSpan={7} className="px-3 py-6 text-center text-gray-500">
                  No sessions.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
```

- [ ] **Step 6:** Create `dashboard/src/pages/project/Sessions.tsx`:

```tsx
import { Link, useParams } from "react-router-dom";
import { useSessions } from "../../api/hooks";

export default function ProjectSessions() {
  const { projectId = "" } = useParams();
  const { data: sessions = [], isLoading } = useSessions(projectId);

  return (
    <div className="space-y-4">
      {isLoading && <p className="text-sm text-gray-400">Loading…</p>}
      <div className="overflow-hidden rounded border border-gray-800">
        <table className="w-full text-sm">
          <thead className="bg-gray-900 text-left text-xs uppercase tracking-wider text-gray-500">
            <tr>
              <th className="px-3 py-2">Name</th>
              <th className="px-3 py-2">Task</th>
              <th className="px-3 py-2">Harness</th>
              <th className="px-3 py-2">State</th>
              <th className="px-3 py-2">Idle</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800">
            {sessions.map((s) => (
              <tr key={s.id} className="hover:bg-gray-900">
                <td className="px-3 py-2">
                  <Link
                    to={`/sessions/${s.id}`}
                    className="text-indigo-400 hover:text-indigo-300"
                  >
                    {s.name}
                  </Link>
                </td>
                <td className="px-3 py-2 text-gray-400">{s.task_id ?? "—"}</td>
                <td className="px-3 py-2 text-gray-400">{s.harness ?? "—"}</td>
                <td className="px-3 py-2">
                  <span
                    className={`rounded px-2 py-0.5 text-xs ${
                      s.stalled ? "bg-amber-500/10 text-amber-400" : "bg-gray-800 text-gray-300"
                    }`}
                  >
                    {s.state ?? "?"}
                  </span>
                </td>
                <td className="px-3 py-2 text-gray-400">
                  {Math.round(s.idle_seconds ?? 0)}s
                </td>
              </tr>
            ))}
            {sessions.length === 0 && !isLoading && (
              <tr>
                <td colSpan={5} className="px-3 py-6 text-center text-gray-500">
                  No sessions for this project.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
```

- [ ] **Step 7:** Wire routes in `dashboard/src/App.tsx`. Import at the top:

```typescript
import SystemSessions from "./pages/system/Sessions";
import ProjectSessions from "./pages/project/Sessions";
import SessionDetail from "./pages/SessionDetail";
```

Add inside `<Route path="system">` alongside events/playbooks:

```tsx
<Route path="sessions" element={<SystemSessions />} />
```

Add inside `<Route path="projects/:projectId" element={<ProjectLayout />}>`:

```tsx
<Route path="sessions" element={<ProjectSessions />} />
```

Add as a top-level route alongside `tasks/:taskId`:

```tsx
<Route path="sessions/:sessionId" element={<SessionDetail />} />
```

- [ ] **Step 8:** Add a "Sessions" tab to `dashboard/src/pages/project/ProjectLayout.tsx` — extend the `tabs` array so it becomes:

```typescript
const tabs: Array<{ to: string; label: string; end?: boolean }> = [
  { to: ".", label: "Overview", end: true },
  { to: "tasks", label: "Tasks" },
  { to: "sessions", label: "Sessions" },
  { to: "workspaces", label: "Workspaces" },
  { to: "profiles", label: "Profiles" },
  { to: "playbooks", label: "Playbooks" },
  { to: "config", label: "Config" },
];
```

- [ ] **Step 9:** Type-check + lint: `npm --prefix dashboard run typecheck && npm --prefix dashboard run lint`. Fix any strict-null issues that surface from the generated types.

- [ ] **Step 10:** Manual smoke test with the daemon running: navigate `/system/sessions`, click through to a session, verify the SSE stream appears in the transcript panel; verify nudge submits (or returns a clean error when no live session exists).

- [ ] **Step 11:** Commit: `feat(dashboard): D1 sessions pages + SSE transcript stream hook`

---

## Task 3: `explain_task` panel + dependency graph tab (D2)

**Files:**
- Create: `dashboard/src/pages/task/TaskGraph.tsx` (renders adjacency of the current task from `get_task_dependencies` — no cytoscape/vis dependency added; a simple ASCII/HTML tree suffices)
- Modify: `dashboard/src/pages/TaskDetail.tsx` (add tabbed "Explain" + "Graph" panels; read the file first to match its component style — do not restructure it beyond adding the tabs)
- Modify: `dashboard/src/api/hooks.ts` (add `useExplainTask`, `useProjectReady`, `useTaskDeps` if missing — `useTaskDeps` may already exist; check first)

**Interfaces:**
- Consumes: SDK functions `explainTask`, `projectReady`, `getTaskDependencies` (or `taskDeps`) generated in Task 1.
- Produces: `useExplainTask(taskId)` returns `ExplainTaskResponse`; `useProjectReady(projectId)` returns `ProjectReadyResponse`; `useTaskDeps(taskId)` returns `TaskDepsResponse`.

- [ ] **Step 1:** Confirm whether `useTaskDeps` already exists by searching `dashboard/src/api/hooks.ts` for `getTaskDependencies` / `taskDeps`. If absent, add it. Add these hooks in `hooks.ts` after the existing task-mutation block:

```typescript
import {
  // ... existing ...
  explainTask,
  projectReady,
  getTaskDependencies,
} from "./client";
import type {
  // ... existing ...
  ExplainTaskResponse,
  ProjectReadyResponse,
  TaskDepsResponse,
} from "./client";

export type { ExplainTaskResponse, ProjectReadyResponse, TaskDepsResponse };

export function useExplainTask(taskId: string) {
  return useQuery({
    queryKey: ["explain", taskId],
    queryFn: async () => {
      const { data } = await explainTask({
        body: { task_id: taskId },
        throwOnError: true,
      });
      return data as ExplainTaskResponse;
    },
    enabled: !!taskId,
    refetchInterval: 20_000,
  });
}

export function useProjectReady(projectId: string) {
  return useQuery({
    queryKey: ["project-ready", projectId],
    queryFn: async () => {
      const { data } = await projectReady({
        body: { project_id: projectId },
        throwOnError: true,
      });
      return data as ProjectReadyResponse;
    },
    enabled: !!projectId,
    refetchInterval: 30_000,
  });
}

export function useTaskDeps(taskId: string) {
  return useQuery({
    queryKey: ["task-deps", taskId],
    queryFn: async () => {
      const { data } = await getTaskDependencies({
        body: { task_id: taskId },
        throwOnError: true,
      });
      return data as TaskDepsResponse;
    },
    enabled: !!taskId,
    refetchInterval: 30_000,
  });
}
```

- [ ] **Step 2:** Create `dashboard/src/pages/task/TaskGraph.tsx`. This renders the immediate dependency neighborhood (upstream + downstream, one hop each — sufficient for MVP; there is no `task_graph` server command, and adding one is out of scope):

```tsx
import { Link } from "react-router-dom";
import { useTaskDeps, type TaskDepsResponse } from "../../api/hooks";

interface Props { taskId: string }

function StatusPill({ status }: { status: string }) {
  const tone =
    status === "COMPLETED" ? "bg-emerald-500/10 text-emerald-400"
    : status === "IN_PROGRESS" ? "bg-indigo-500/10 text-indigo-400"
    : status === "BLOCKED" ? "bg-amber-500/10 text-amber-400"
    : status === "FAILED" ? "bg-red-500/10 text-red-400"
    : "bg-gray-800 text-gray-300";
  return <span className={`rounded px-2 py-0.5 text-xs ${tone}`}>{status || "?"}</span>;
}

function TaskList({
  title, items,
}: { title: string; items: TaskDepsResponse["depends_on"] }) {
  return (
    <div className="rounded border border-gray-800 bg-gray-950 p-3">
      <h3 className="mb-2 text-sm font-semibold text-gray-300">
        {title} <span className="text-xs text-gray-500">({items?.length ?? 0})</span>
      </h3>
      {(!items || items.length === 0) ? (
        <p className="text-xs text-gray-500">None.</p>
      ) : (
        <ul className="space-y-1 text-sm">
          {items.map((t) => (
            <li key={t.id} className="flex items-center justify-between gap-2">
              <Link
                to={`/tasks/${t.id}`}
                className="truncate font-mono text-indigo-400 hover:text-indigo-300"
              >
                {t.id}
              </Link>
              <span className="flex-1 truncate text-gray-400">{t.title}</span>
              <StatusPill status={t.status} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function TaskGraph({ taskId }: Props) {
  const { data, isLoading, error } = useTaskDeps(taskId);
  if (isLoading) return <p className="text-sm text-gray-400">Loading graph…</p>;
  if (error) return <p className="text-sm text-red-400">{(error as Error).message}</p>;
  if (!data) return null;
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <TaskList title="Depends on (upstream)" items={data.depends_on ?? []} />
      <TaskList title="Blocks (downstream)" items={data.blocks ?? []} />
    </div>
  );
}
```

- [ ] **Step 3:** Create a small "Explain" panel component alongside the graph. Add it to `dashboard/src/pages/task/TaskGraph.tsx` (same file to keep the diff local), or as `TaskExplain.tsx` — either is fine. Component:

```tsx
import { useExplainTask } from "../../api/hooks";

export function TaskExplain({ taskId }: { taskId: string }) {
  const { data, isLoading, error } = useExplainTask(taskId);
  if (isLoading) return <p className="text-sm text-gray-400">Loading reasons…</p>;
  if (error) return <p className="text-sm text-red-400">{(error as Error).message}</p>;
  if (!data) return null;
  const reasons = data.reasons ?? [];
  if (reasons.length === 0) {
    return (
      <p className="text-sm text-emerald-400">
        No blockers — this task is ready or already running.
      </p>
    );
  }
  return (
    <ul className="space-y-2">
      {reasons.map((r, idx) => (
        <li
          key={`${r.code}-${idx}`}
          className="rounded border border-gray-800 bg-gray-950 p-3 text-sm"
        >
          <div className="mb-1 flex items-center gap-2">
            <span className="rounded bg-amber-500/10 px-2 py-0.5 text-xs text-amber-400">
              {r.code}
            </span>
            {r.ref && (
              <span className="font-mono text-xs text-gray-500">ref: {r.ref}</span>
            )}
          </div>
          <p className="text-gray-300">{r.detail}</p>
        </li>
      ))}
    </ul>
  );
}
```

Update the import in `TaskGraph.tsx` file's default export section so `TaskExplain` is exported as a named export.

- [ ] **Step 4:** Modify `dashboard/src/pages/TaskDetail.tsx` to add two new tabs "Explain" and "Graph". Before editing, read that file to see how it currently renders. Add near the top:

```typescript
import TaskGraph, { TaskExplain } from "./task/TaskGraph";
```

Add a local tab state (or expand the existing tabs if present):

```tsx
// Add these two entries to the existing tabs array (or introduce one if
// TaskDetail is currently a single-view page — match the component's own
// pattern, do not rewrite the page).
{ id: "explain", label: "Explain" },
{ id: "graph",   label: "Graph"   },
```

Render conditionally:

```tsx
{activeTab === "explain" && <TaskExplain taskId={taskId} />}
{activeTab === "graph"   && <TaskGraph   taskId={taskId} />}
```

If `TaskDetail.tsx` has no tab state today, add the minimum: `const [activeTab, setActiveTab] = useState<"details" | "explain" | "graph">("details");` plus a small tabs bar reusing the same NavLink-style pattern from `ProjectLayout.tsx`.

- [ ] **Step 5:** `npm --prefix dashboard run typecheck && npm --prefix dashboard run lint`.

- [ ] **Step 6:** Manual smoke: create a blocked task in the daemon; navigate to `/tasks/<id>` → Explain tab; see the `blocked_dependency`/`blocked_gate`/capacity reason list. Graph tab shows upstream + downstream.

- [ ] **Step 7:** Commit: `feat(dashboard): D2 task explain + dependency graph tabs`

---

## Task 4: Gates inbox page (D3)

**Files:**
- Modify: `dashboard/src/api/hooks.ts` (gate hooks)
- Create: `dashboard/src/pages/system/Gates.tsx`
- Modify: `dashboard/src/App.tsx` (route)
- Modify: `dashboard/src/components/Layout.tsx` (nav link)

**Interfaces:**
- Consumes: SDK `gateList`, `gateShow`, `gateResolve` from Task 1.
- Produces: `useGates(opts)`, `useResolveGate()`.

- [ ] **Step 1:** Add hooks to `dashboard/src/api/hooks.ts`:

```typescript
import {
  // ... existing ...
  gateList,
  gateShow,
  gateResolve,
} from "./client";
import type {
  // ... existing ...
  GateListResponse,
  GateShowResponse,
  GateResolveResponse,
  GateSummary,
} from "./client";

export type { GateSummary, GateListResponse };

export function useGates(opts: { projectId?: string; status?: string; gateType?: string } = {}) {
  return useQuery({
    queryKey: ["gates", opts.projectId ?? "all", opts.status ?? "any", opts.gateType ?? "any"],
    queryFn: async () => {
      const body: Record<string, unknown> = {};
      if (opts.projectId) body.project_id = opts.projectId;
      if (opts.status) body.status = opts.status;
      if (opts.gateType) body.gate_type = opts.gateType;
      const { data } = await gateList({ body, throwOnError: true });
      return ((data as GateListResponse).gates ?? []) as GateSummary[];
    },
    refetchInterval: 20_000,
  });
}

export function useGate(gateId: string) {
  return useQuery({
    queryKey: ["gate", gateId],
    queryFn: async () => {
      const { data } = await gateShow({
        body: { gate_id: gateId },
        throwOnError: true,
      });
      return data as GateShowResponse;
    },
    enabled: !!gateId,
  });
}

export function useResolveGate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: {
      gate_id: string;
      resolved_by: string;
      resolution?: string;
    }) => (await gateResolve({ body: input, throwOnError: true })).data as GateResolveResponse,
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ["gates"] });
      queryClient.invalidateQueries({ queryKey: ["gate", variables.gate_id] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}
```

- [ ] **Step 2:** Create `dashboard/src/pages/system/Gates.tsx`:

```tsx
import { useState } from "react";
import { CheckIcon } from "@heroicons/react/24/outline";
import { useGates, useResolveGate, type GateSummary } from "../../api/hooks";

const STATUSES = ["open", "resolved", "expired"] as const;

function GateRow({ gate }: { gate: GateSummary }) {
  const resolve = useResolveGate();
  const [resolution, setResolution] = useState("");
  const [showForm, setShowForm] = useState(false);

  return (
    <tr className="hover:bg-gray-900">
      <td className="px-3 py-2 font-mono text-xs text-gray-400">{gate.id}</td>
      <td className="px-3 py-2 text-gray-400">{gate.project_id}</td>
      <td className="px-3 py-2 text-gray-300">{gate.gate_type}</td>
      <td className="px-3 py-2">
        <div className="text-gray-200">{gate.title}</div>
        {gate.question && <div className="text-xs text-gray-500">{gate.question}</div>}
      </td>
      <td className="px-3 py-2">
        <span
          className={`rounded px-2 py-0.5 text-xs ${
            gate.status === "open"
              ? "bg-amber-500/10 text-amber-400"
              : gate.status === "resolved"
              ? "bg-emerald-500/10 text-emerald-400"
              : "bg-gray-800 text-gray-400"
          }`}
        >
          {gate.status}
        </span>
      </td>
      <td className="px-3 py-2 text-right">
        {gate.status === "open" && (
          <>
            {!showForm && (
              <button
                onClick={() => setShowForm(true)}
                className="inline-flex items-center gap-1 rounded bg-emerald-600 px-2 py-1 text-xs font-medium text-white hover:bg-emerald-500"
              >
                <CheckIcon className="h-3 w-3" />
                Resolve
              </button>
            )}
            {showForm && (
              <form
                className="flex gap-1"
                onSubmit={(e) => {
                  e.preventDefault();
                  resolve.mutate(
                    {
                      gate_id: gate.id,
                      resolved_by: "dashboard",
                      resolution,
                    },
                    { onSuccess: () => setShowForm(false) },
                  );
                }}
              >
                <input
                  value={resolution}
                  onChange={(e) => setResolution(e.target.value)}
                  placeholder="Resolution note…"
                  className="rounded border border-gray-800 bg-gray-900 px-2 py-1 text-xs text-gray-200"
                />
                <button
                  type="submit"
                  disabled={resolve.isPending}
                  className="rounded bg-emerald-600 px-2 py-1 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
                >
                  OK
                </button>
                <button
                  type="button"
                  onClick={() => setShowForm(false)}
                  className="rounded border border-gray-800 px-2 py-1 text-xs text-gray-400"
                >
                  Cancel
                </button>
              </form>
            )}
          </>
        )}
      </td>
    </tr>
  );
}

export default function SystemGates() {
  const [status, setStatus] = useState<string>("open");
  const { data: gates = [], isLoading, error } = useGates({ status });

  return (
    <div className="space-y-4">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-bold">Gates</h1>
          <p className="text-sm text-gray-500">
            Work-graph gates awaiting resolution — resolving one may unblock waiter tasks.
          </p>
        </div>
        <div className="flex gap-1">
          {STATUSES.map((s) => (
            <button
              key={s}
              onClick={() => setStatus(s)}
              className={`rounded px-3 py-1 text-xs font-medium ${
                s === status
                  ? "bg-indigo-600 text-white"
                  : "border border-gray-800 text-gray-400 hover:bg-gray-900"
              }`}
            >
              {s}
            </button>
          ))}
        </div>
      </header>

      {isLoading && <p className="text-sm text-gray-400">Loading…</p>}
      {error && (
        <p className="text-sm text-red-400">
          Failed to load gates: {(error as Error).message}
        </p>
      )}

      <div className="overflow-hidden rounded border border-gray-800">
        <table className="w-full text-sm">
          <thead className="bg-gray-900 text-left text-xs uppercase tracking-wider text-gray-500">
            <tr>
              <th className="px-3 py-2">ID</th>
              <th className="px-3 py-2">Project</th>
              <th className="px-3 py-2">Type</th>
              <th className="px-3 py-2">Title / Question</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2 text-right">Action</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800">
            {gates.map((g) => <GateRow key={g.id} gate={g} />)}
            {gates.length === 0 && !isLoading && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-gray-500">
                  No gates.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
```

- [ ] **Step 3:** Add route in `dashboard/src/App.tsx`:

```typescript
import SystemGates from "./pages/system/Gates";
```

Inside `<Route path="system">`:

```tsx
<Route path="gates" element={<SystemGates />} />
```

- [ ] **Step 4:** Add a "Gates" nav item in `dashboard/src/components/Layout.tsx` in the same style as "Events" / "Sessions". Read the file first to match the pattern.

- [ ] **Step 5:** `npm --prefix dashboard run typecheck && npm --prefix dashboard run lint`.

- [ ] **Step 6:** Manual smoke: create a gate via `aq gate create --project-id ... --gate-type manual --title "test"`; verify it appears in `/system/gates` with `status=open`; click Resolve, enter note, submit; row disappears from the `open` filter.

- [ ] **Step 7:** Commit: `feat(dashboard): D3 gates inbox page`

---

## Task 5: Supervisor chat page (D4)

**Files:**
- Create: `dashboard/src/api/chat.ts` (thin wrapper around `/api/sessions/{name}/message` + `/messages` — these are not codegen routes, so we hand-write a small fetch helper that piggybacks on the interceptor via a full URL)
- Modify: `dashboard/src/api/hooks.ts` (chat hooks: `useChatMessages`, `useSendChatMessage`)
- Create: `dashboard/src/pages/project/Chat.tsx`
- Modify: `dashboard/src/App.tsx` (route)
- Modify: `dashboard/src/pages/project/ProjectLayout.tsx` (tab)
- Modify: `dashboard/src/ws/useEventStream.ts` (add `message.*` invalidation) — see Task 6 for the full change; if Task 6 is landed first, this task piggybacks.

**Interfaces:**
- Consumes: existing `POST /api/sessions/{name}/message` (request body: `{body, from, from_kind, thread_id?, subject?, priority?}`); `GET /api/sessions/{name}/messages?thread_id&since&limit&include_archived` (returns `{success, session, project_id, count, messages: [MessageModel]}`). `name` = `supervisor-<projectId>`.
- Produces: `useChatMessages(projectId)`, `useSendChatMessage(projectId)`.

- [ ] **Step 1:** Create `dashboard/src/api/chat.ts`:

```typescript
/**
 * Chat wire — talks to /api/sessions/supervisor-<pid>/(message[s]).
 * These endpoints are not part of the codegen client (path carries the
 * session name), so we call them by URL. The generated client's baseUrl
 * config is honored via the same env-var precedence.
 */

import type { MessageModel } from "./client";

function baseUrl(): string {
  return import.meta.env.VITE_API_URL || "";
}

function supervisorName(projectId: string): string {
  return `supervisor-${projectId}`;
}

async function throwing(resp: Response): Promise<Response> {
  if (resp.ok) return resp;
  let detail: string;
  try {
    const body = await resp.clone().json();
    detail = typeof body?.detail === "string" ? body.detail
      : typeof body?.error === "string" ? body.error
      : JSON.stringify(body);
  } catch {
    detail = await resp.clone().text();
  }
  throw new Error(`API ${resp.status}: ${detail}`);
}

export interface ChatMessagesResponse {
  success: boolean;
  session: string;
  project_id: string;
  count: number;
  messages: MessageModel[];
}

export async function fetchChatMessages(
  projectId: string,
  opts: { since?: number; limit?: number; threadId?: string } = {},
): Promise<ChatMessagesResponse> {
  const params = new URLSearchParams();
  if (opts.since !== undefined) params.set("since", String(opts.since));
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  if (opts.threadId) params.set("thread_id", opts.threadId);
  const url = `${baseUrl()}/api/sessions/${encodeURIComponent(
    supervisorName(projectId),
  )}/messages${params.toString() ? `?${params}` : ""}`;
  const resp = await throwing(await fetch(url));
  return (await resp.json()) as ChatMessagesResponse;
}

export interface SendChatMessageResponse {
  success: boolean;
  message_id: string;
  state: string;
}

export async function sendChatMessage(
  projectId: string,
  body: string,
  from: string = "dashboard",
): Promise<SendChatMessageResponse> {
  const url = `${baseUrl()}/api/sessions/${encodeURIComponent(
    supervisorName(projectId),
  )}/message`;
  const resp = await throwing(
    await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ body, from, from_kind: "user" }),
    }),
  );
  return (await resp.json()) as SendChatMessageResponse;
}
```

- [ ] **Step 2:** Add hooks in `dashboard/src/api/hooks.ts`:

```typescript
import {
  fetchChatMessages,
  sendChatMessage,
  type ChatMessagesResponse,
} from "./chat";

export function useChatMessages(projectId: string, limit = 200) {
  return useQuery({
    queryKey: ["chat", projectId, limit],
    queryFn: () => fetchChatMessages(projectId, { limit }),
    enabled: !!projectId,
    refetchInterval: 15_000,
  });
}

export function useSendChatMessage(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: string) => sendChatMessage(projectId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["chat", projectId] });
    },
  });
}

export type { ChatMessagesResponse };
```

- [ ] **Step 3:** Create `dashboard/src/pages/project/Chat.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { PaperAirplaneIcon } from "@heroicons/react/24/outline";
import { useChatMessages, useSendChatMessage } from "../../api/hooks";
import type { MessageModel } from "../../api/client";

function fmtTime(ts: number | null | undefined): string {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString();
}

function Bubble({ msg }: { msg: MessageModel }) {
  const mine = msg.from_kind === "user";
  return (
    <div className={`flex ${mine ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[80%] rounded-lg px-3 py-2 text-sm ${
          mine
            ? "bg-indigo-600/20 text-indigo-100"
            : "bg-gray-800 text-gray-100"
        }`}
      >
        <div className="mb-1 flex items-center gap-2 text-xs text-gray-400">
          <span className="font-mono">{msg.from_ ?? `${msg.from_kind}:${msg.from_id}`}</span>
          <span>{fmtTime(msg.created_at)}</span>
        </div>
        <div className="whitespace-pre-wrap">{msg.body}</div>
      </div>
    </div>
  );
}

export default function ProjectChat() {
  const { projectId = "" } = useParams();
  const { data, isLoading, error } = useChatMessages(projectId);
  const send = useSendChatMessage(projectId);
  const [body, setBody] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [data]);

  const messages = data?.messages ?? [];

  return (
    <div className="flex h-[calc(100vh-14rem)] flex-col space-y-3">
      <header>
        <h2 className="text-lg font-semibold">Chat with supervisor</h2>
        <p className="text-xs text-gray-500">
          Talking to <span className="font-mono">supervisor-{projectId}</span>.
          Messages appear here as the session replies.
        </p>
      </header>

      {error && (
        <p className="text-sm text-red-400">
          Failed to load chat: {(error as Error).message}
        </p>
      )}

      <div
        ref={scrollRef}
        className="flex-1 space-y-2 overflow-y-auto rounded border border-gray-800 bg-gray-950 p-3"
      >
        {isLoading && <p className="text-sm text-gray-500">Loading…</p>}
        {messages.length === 0 && !isLoading && (
          <p className="text-sm text-gray-500">No messages yet — say hello.</p>
        )}
        {messages.map((m) => <Bubble key={m.id} msg={m} />)}
      </div>

      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (!body.trim() || send.isPending) return;
          send.mutate(body, { onSuccess: () => setBody("") });
        }}
      >
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              if (body.trim() && !send.isPending) {
                send.mutate(body, { onSuccess: () => setBody("") });
              }
            }
          }}
          rows={2}
          placeholder="Message the supervisor (Cmd/Ctrl+Enter to send)…"
          className="flex-1 resize-none rounded border border-gray-800 bg-gray-900 px-2 py-1 text-sm text-gray-200"
        />
        <button
          type="submit"
          disabled={send.isPending || !body.trim()}
          className="inline-flex items-center gap-1 rounded bg-indigo-600 px-3 py-1 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
        >
          <PaperAirplaneIcon className="h-4 w-4" />
          Send
        </button>
      </form>
      {send.error && (
        <p className="text-xs text-red-400">{(send.error as Error).message}</p>
      )}
    </div>
  );
}
```

- [ ] **Step 4:** Register the route in `dashboard/src/App.tsx`:

```typescript
import ProjectChat from "./pages/project/Chat";
```

Inside `<Route path="projects/:projectId" element={<ProjectLayout />}>`:

```tsx
<Route path="chat" element={<ProjectChat />} />
```

- [ ] **Step 5:** Add "Chat" tab in `dashboard/src/pages/project/ProjectLayout.tsx` — insert after "Sessions":

```typescript
  { to: "chat", label: "Chat" },
```

- [ ] **Step 6:** `npm --prefix dashboard run typecheck && npm --prefix dashboard run lint`.

- [ ] **Step 7:** Manual smoke: with a supervisor session running for a project (or with `messages.enabled: true` in config so the row is queued regardless), send a message from the page; verify a row appears in `aq message list --project-id <pid>`.

- [ ] **Step 8:** Commit: `feat(dashboard): D4 supervisor chat page`

---

## Task 6: WS invalidation for gate/message/session + after_seq resume

**Files:**
- Modify: `dashboard/src/ws/types.ts` (new event interfaces)
- Modify: `dashboard/src/ws/useEventStream.ts` (invalidation cases + `after_seq` persistence)

**Interfaces:**
- Consumes: registered event schemas (verified in `src/event_schemas.py`): `gate.created`, `gate.resolved`, `gate.expired`, `message.sent`, `message.delivered`, `message.replied`, `session.started`, `session.exited`, `session.adopted`, `task.blocked`, `task.unblocked`. Server-side WS forwards `notify.*` and `message.*` (`src/api/websocket.py:33`); `gate.*` and `session.*` are on the internal bus but **not** forwarded by the current filter — for those to reach the dashboard, this task also flips the WS filter to include them.

- Produces: typed events + invalidation; `?after_seq=N` query param sent on reconnect, `N` persisted in `localStorage` under key `aq:ws:last_seq`.

- [ ] **Step 1:** Widen the server-side WS forward filter to include `gate.*` and `session.*`. In `src/api/websocket.py`:

```python
# Event-type prefixes forwarded to WebSocket clients.  Extended (D3/D1) so
# the dashboard's gates inbox and sessions pages react to bus events.
_FORWARDED_PREFIXES: tuple[str, ...] = (
    "notify.", "message.", "gate.", "session.", "task.",
)
```

- [ ] **Step 2:** Add typed events to `dashboard/src/ws/types.ts` (append):

```typescript
// --- Gate lifecycle ---

export interface GateCreatedEvent extends BaseEvent {
  event_type: "gate.created";
  gate_id: string;
  gate_type: string;
  title: string;
}

export interface GateResolvedEvent extends BaseEvent {
  event_type: "gate.resolved";
  gate_id: string;
  resolved_by: string;
  unblocked_task_ids?: string[];
}

export interface GateExpiredEvent extends BaseEvent {
  event_type: "gate.expired";
  gate_id: string;
}

// --- Message lifecycle ---

export interface MessageSentEvent extends BaseEvent {
  event_type: "message.sent";
  message_id: string;
  to_kind: string;
  to_id: string;
}

export interface MessageDeliveredEvent extends BaseEvent {
  event_type: "message.delivered";
  message_id: string;
  method?: string;
}

export interface MessageRepliedEvent extends BaseEvent {
  event_type: "message.replied";
  message_id: string;
  reply_id: string;
}

// --- Session lifecycle ---

export interface SessionStartedEvent extends BaseEvent {
  event_type: "session.started";
  session_id: string;
  name: string;
  task_id?: string;
}

export interface SessionExitedEvent extends BaseEvent {
  event_type: "session.exited";
  session_id: string;
  name: string;
  verdict: string;
}

export interface SessionAdoptedEvent extends BaseEvent {
  event_type: "session.adopted";
  session_id: string;
  name: string;
}

// --- Task blocked/unblocked (work-graph) ---

export interface TaskBlockedGraphEvent extends BaseEvent {
  event_type: "task.blocked";
  task_id: string;
  project_id: string;
  title: string;
  reason?: string;
}

export interface TaskUnblockedEvent extends BaseEvent {
  event_type: "task.unblocked";
  task_id: string;
  project_id: string;
  title: string;
  reason?: string;
}
```

Extend the `NotifyEvent` union at the bottom:

```typescript
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
  | TaskUnblockedEvent;
```

- [ ] **Step 3:** Modify `dashboard/src/ws/useEventStream.ts` — add `after_seq` persistence and new invalidation cases. Replace the module-level `connect()` function and `handleEvent` switch:

```typescript
const LAST_SEQ_KEY = "aq:ws:last_seq";

function loadLastSeq(): number | null {
  try {
    const raw = localStorage.getItem(LAST_SEQ_KEY);
    if (raw == null) return null;
    const n = Number.parseInt(raw, 10);
    return Number.isFinite(n) ? n : null;
  } catch {
    return null;
  }
}

function saveLastSeq(seq: number): void {
  try { localStorage.setItem(LAST_SEQ_KEY, String(seq)); } catch { /* ignore */ }
}

function connect() {
  if (ws?.readyState === WebSocket.OPEN || ws?.readyState === WebSocket.CONNECTING) {
    return;
  }

  const wsBase = import.meta.env.VITE_WS_URL
    || `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`;
  const lastSeq = loadLastSeq();
  const qs = lastSeq != null ? `?after_seq=${lastSeq}` : "";
  const url = `${wsBase}/ws/events${qs}`;

  setStatus("connecting");
  const sock = new WebSocket(url);
  ws = sock;

  sock.onopen = () => {
    reconnectDelay = BASE_RECONNECT_MS;
    setStatus("connected");
  };

  sock.onmessage = (msg) => {
    try {
      const event = JSON.parse(msg.data) as NotifyEvent & { seq?: number | null };
      if (typeof event.seq === "number") saveLastSeq(event.seq);
      for (const fn of eventListeners) fn(event);
    } catch {
      // ignore
    }
  };

  sock.onclose = () => {
    ws = null;
    setStatus("disconnected");
    setTimeout(() => {
      reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_MS);
      connect();
    }, reconnectDelay);
  };

  sock.onerror = () => { /* onclose handles reconnect */ };
}
```

Extend the switch in `handleEvent`:

```typescript
        case "gate.created":
        case "gate.resolved":
        case "gate.expired":
          queryClient.invalidateQueries({ queryKey: ["gates"] });
          queryClient.invalidateQueries({ queryKey: ["gate"] });
          // Resolution may unblock tasks — refresh task views too.
          queryClient.invalidateQueries({ queryKey: ["tasks"] });
          queryClient.invalidateQueries({ queryKey: ["explain"] });
          break;

        case "message.sent":
        case "message.delivered":
        case "message.replied":
          queryClient.invalidateQueries({ queryKey: ["chat"] });
          break;

        case "session.started":
        case "session.exited":
        case "session.adopted":
          queryClient.invalidateQueries({ queryKey: ["sessions"] });
          queryClient.invalidateQueries({ queryKey: ["session", event.session_id] });
          break;

        case "task.blocked":
        case "task.unblocked":
          queryClient.invalidateQueries({ queryKey: ["tasks"] });
          queryClient.invalidateQueries({ queryKey: ["task", event.task_id] });
          queryClient.invalidateQueries({ queryKey: ["explain", event.task_id] });
          break;
```

Ensure the `switch` still has a default no-op branch and remains type-safe. Because the new events have new `event_type` string literals, TypeScript may require narrowing helpers; if the union grows past what a plain `switch` can narrow, use `if (type.startsWith("gate."))` style guards for those cases instead.

- [ ] **Step 4:** Verify the invalidation with a manual test: with the dashboard open, from a second terminal run `aq gate create ...` — the Gates page updates without a manual refresh. Resolve the gate; the row moves out of `open`.

- [ ] **Step 5:** `npm --prefix dashboard run typecheck && npm --prefix dashboard run lint`.

- [ ] **Step 6:** Small backend test for the filter widening. Extend or add `tests/test_websocket_forwarding.py` (create if missing; if a similar test exists, extend it):

```python
"""Guard: gate.*/session.*/task.* events reach WebSocket clients (D3/D1/D2)."""

from __future__ import annotations

import asyncio

import pytest

from src.api.websocket import _FORWARDED_PREFIXES


def test_forwarded_prefixes_include_wave4_events() -> None:
    for prefix in ("notify.", "message.", "gate.", "session.", "task."):
        assert prefix in _FORWARDED_PREFIXES, (
            f"Prefix '{prefix}' must be forwarded to WebSocket clients — "
            "the dashboard's gates/sessions/tasks pages rely on it for "
            "live invalidation."
        )
```

Run: `pytest tests/test_websocket_forwarding.py -v`.

- [ ] **Step 7:** Full suite regression: `pytest tests/ -n auto` (zero NEW failures).

- [ ] **Step 8:** Commit: `feat(dashboard): WS invalidation for gate/message/session + after_seq resume`

---

## Self-review

- **No placeholders:** every step names concrete files, exact command names, exact response fields, exact WebSocket keys. No "TBD", no "similar to Task N", no "add appropriate handling".
- **Type consistency:** response models mirror the actual `_cmd_*` return dicts verified by reading the source (`_cmd_session_list`, `_cmd_session_peek`, `_cmd_session_attach`, `_cmd_session_nudge`, `_cmd_session_logs`, `_cmd_gate_*`, `_cmd_explain_task`, `_cmd_project_ready`, `_cmd_task_deps`, `_cmd_message_*`). `extra="allow"` is used where the underlying command returns extra keys (peek fallback merged into logs; provider-specific kill payloads; gate rows carrying resolved-at metadata; message dicts with `from_`/`to` conveniences).
- **Codegen categorization is decisive:** sessions go under `system` (no new CategoryMeta; existing category, verified at `src/tools/registry.py:96`); gates/explain/project_ready under `task`; message_* already under `message`. Every categorized command has a response model or is in `API_EXCLUDED` — Task 1 lands a parametrized guard test that will fail if a future command is added without a model.
- **`message_send` handling is explicit:** stays in `API_EXCLUDED`; chat page routes via the existing `POST /api/sessions/{name}/message` (`src/api/messages.py`, verified body shape `{body, from, from_kind, ...}`); no duplicate route.
- **WS forwarding widened deliberately:** `_FORWARDED_PREFIXES` currently only lets `notify.*` and `message.*` through, so `gate.*` and `session.*` never reached the dashboard. Task 6 widens the filter and adds a guard test so a future narrowing would break CI.
- **Task graph out of scope:** no `task_graph` server command exists (verified — `create_task_graph` is unrelated; it's the plan-parser side). D2's Graph tab renders `get_task_dependencies` one-hop neighborhood without new backend work; the plan explicitly notes this instead of promising a richer graph command.
- **SSE hook is new and load-bearing:** `useTranscriptStream` reads from the existing endpoint at `src/api/sessions.py:108`, verified frame shape.
- **`after_seq` resume:** server already supports the query param (`src/api/websocket.py:83`); client changes are additive.
- **Tasks are sized for isolation:** each task's context (files, interfaces, code) is self-contained; a fresh subagent picking up Task 3 doesn't need to have done Task 2 (except the SDK regeneration in Task 1 as a prerequisite — noted at the top of each frontend task via the `client.ts` imports).
