# Dashboard v2 Phase 3: Shell + Chat Landing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the dashboard nav to four top-level sections (Chat, Command Center, Work, Settings), promote supervisor chat to the landing page with WS-driven live replies and inline event cards, and add a phone-first responsive shell — no backend changes.

**Architecture:** Pure frontend refactor of `dashboard/src/`. Existing pages are moved (not rewritten) under `/settings/*` and `/work/*` route branches; the sidebar is replaced by a nav component with a desktop rail (`md+`) and a mobile bottom-nav (`<md`). Chat lives at `/` and per-project at `/chat/:projectId`, wiring the existing `/api/sessions/supervisor-{pid}/message[s]` endpoints via the existing `fetchChatMessages`/`sendChatMessage` helpers plus a new WS subscription that filters `message.sent` events by `thread_id = "dashboard:<projectId>"`. Command Center is a placeholder page — Phase 4 fills it. Old routes redirect.

**Tech Stack:** React 19, React Router v7, TanStack Query v5, Tailwind v4, TypeScript 5.7, Vite 6, `@aq/ts-client` generated SDK, `@heroicons/react`, `react-markdown`.

## Global Constraints

- No new dependencies. Reuse `@heroicons/react/24/outline`, `react-markdown`, existing `@aq/ts-client` SDK.
- All daemon I/O goes through `dashboard/src/api/client.ts` SDK functions, `dashboard/src/api/chat.ts` helpers, or the existing WS singleton — never raw `fetch` in components.
- No route or page is deleted. Everything under old paths (`/system/*`, `/projects/:pid/*`) either moves or gets a redirect; existing detail routes (`/tasks/:id`, `/sessions/:id`, `/playbooks/:id`) stay live at their current paths.
- Tailwind v4 responsive tokens only: `md:` = 768px+ desktop, default (mobile) = <768px. Chat must be usable at 390px viewport width (iPhone 14 baseline).
- Chat message wire: `POST /api/sessions/supervisor-<pid>/message` body `{ body, from: "dashboard", from_kind: "user", thread_id: "dashboard:<pid>" }`. Supervisor replies arrive as `message.sent` WS events with `to_kind:"user"`, `to_id:"dashboard"`, `thread_id:"dashboard:<pid>"`, plus a `reply_to_id` referring to the outbound message.
- The dashboard package has no test tooling (no `vitest`, no `@testing-library/*` in `package.json`, no `tests/` directory). Verification uses `npm run typecheck`, `npm run lint`, `npm run build`, and manual dev-server checks at 1280px and 390px viewport widths. Do NOT introduce a test framework in this plan.
- Verification commands run from `dashboard/`: `AQ_API_TARGET=http://127.0.0.1:8091 npm run dev`, `npm run typecheck`, `npm run lint`, `npm run build`.
- Frequent commits: one commit per completed task, message prefix `feat(dashboard):` or `refactor(dashboard):` or `chore(dashboard):`.

---

## File Structure

**New files:**
- `dashboard/src/components/nav/AppShell.tsx` — replaces `Layout.tsx` content (renders sidebar OR bottom-nav based on breakpoint, wraps `<Outlet />`).
- `dashboard/src/components/nav/DesktopSidebar.tsx` — new four-section desktop rail (Chat / Command Center / Work / Settings) plus project list.
- `dashboard/src/components/nav/MobileBottomNav.tsx` — fixed bottom-nav with four icons, visible on `<md` only.
- `dashboard/src/components/nav/SettingsSidebar.tsx` — secondary nav shown inside `/settings/*` (Playbooks, Profiles, Intelligence Classes, Config).
- `dashboard/src/pages/chat/ChatLanding.tsx` — `/` route: project picker (grid of project cards) that navigates to `/chat/:projectId`.
- `dashboard/src/pages/chat/ChatConversation.tsx` — `/chat/:projectId` route: transcript + composer + inline event cards. Phone-first.
- `dashboard/src/pages/chat/InlineEventCard.tsx` — compact rendering of `task.started/completed`, `gate.created/resolved`, `notify.playbook_run_failed` with deep-links.
- `dashboard/src/pages/chat/useChatTranscript.ts` — hook: hydrates via `fetchChatMessages(projectId, { threadId: "dashboard:<pid>" })`, appends WS `message.sent` frames matching the thread, tracks optimistic in-flight sends, interleaves recent events chronologically.
- `dashboard/src/pages/command-center/CommandCenterPlaceholder.tsx` — `/command-center` placeholder page ("Coming in Phase 4"); links back to Chat/Work.
- `dashboard/src/pages/work/WorkIndex.tsx` — `/work` combined filterable tasks + agents view (reuses existing hooks; renders both tables stacked).
- `dashboard/src/pages/work/WorkTasks.tsx` — extracted task table (moved from `pages/project/Tasks.tsx` table body; supports optional projectId filter).
- `dashboard/src/pages/work/WorkAgents.tsx` — agent table extracted / reused from `pages/system/Overview.tsx` agent list.
- `dashboard/src/pages/settings/SettingsLayout.tsx` — sidebar + `<Outlet />` for settings pages.
- `dashboard/src/pages/settings/IntelligenceClassesStub.tsx` — stub page listing "Intelligence Classes — populated by Phase 1 backend" with an empty state.
- `dashboard/src/hooks/useMediaQuery.ts` — small hook, returns `boolean` for a media-query string.

**Modified files:**
- `dashboard/src/App.tsx` — new route tree (see Task 3).
- `dashboard/src/components/Layout.tsx` — becomes a thin wrapper delegating to `AppShell` (kept so existing imports don't break during migration).
- `dashboard/src/components/Sidebar.tsx` — deleted at end of migration; kept until Task 3 is committed.

---

## Task 1: Media-query hook + AppShell scaffolding

**Files:**
- Create: `dashboard/src/hooks/useMediaQuery.ts`
- Create: `dashboard/src/components/nav/AppShell.tsx`
- Create: `dashboard/src/components/nav/DesktopSidebar.tsx`
- Create: `dashboard/src/components/nav/MobileBottomNav.tsx`
- Modify: `dashboard/src/components/Layout.tsx`

**Interfaces:**
- Consumes: `useProjects` / `useOrchestratorStatus` from `../../api/hooks` (as `Sidebar.tsx` already does).
- Produces:
  - `useMediaQuery(query: string): boolean`
  - `<AppShell />` — renders `DesktopSidebar` at `md+` (via CSS `hidden md:flex`), `MobileBottomNav` at `<md` (via `md:hidden`), and `<Outlet />` in a scrollable main area. Both nav variants are always in the DOM; visibility is CSS-driven so hydration doesn't flicker.
  - Nav sections (both variants): `[{to:"/", label:"Chat", icon:ChatBubbleLeftRightIcon, end:true}, {to:"/command-center", label:"Center", icon:Squares2X2Icon}, {to:"/work", label:"Work", icon:BriefcaseIcon}, {to:"/settings", label:"Settings", icon:Cog6ToothIcon}]`.

- [ ] **Step 1: Create `useMediaQuery` hook**

```typescript
// dashboard/src/hooks/useMediaQuery.ts
import { useEffect, useState } from "react";

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => {
    if (typeof window === "undefined") return false;
    return window.matchMedia(query).matches;
  });

  useEffect(() => {
    const mql = window.matchMedia(query);
    const onChange = (e: MediaQueryListEvent) => setMatches(e.matches);
    setMatches(mql.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}
```

- [ ] **Step 2: Create `DesktopSidebar`**

```tsx
// dashboard/src/components/nav/DesktopSidebar.tsx
import { NavLink } from "react-router-dom";
import {
  ChatBubbleLeftRightIcon,
  Squares2X2Icon,
  BriefcaseIcon,
  Cog6ToothIcon,
  CpuChipIcon,
  FolderIcon,
} from "@heroicons/react/24/outline";
import { useOrchestratorStatus, useProjects } from "../../api/hooks";

const sections = [
  { to: "/", label: "Chat", icon: ChatBubbleLeftRightIcon, end: true },
  { to: "/command-center", label: "Command Center", icon: Squares2X2Icon },
  { to: "/work", label: "Work", icon: BriefcaseIcon },
  { to: "/settings", label: "Settings", icon: Cog6ToothIcon },
];

export default function DesktopSidebar() {
  const { data: projects } = useProjects();
  const { data: orch } = useOrchestratorStatus();
  const paused = orch?.status === "paused";
  return (
    <aside className="hidden w-60 shrink-0 flex-col border-r border-gray-800 bg-gray-900 md:flex">
      <div className="flex h-14 items-center gap-2 border-b border-gray-800 px-4">
        <CpuChipIcon className="h-6 w-6 text-indigo-400" />
        <span className="text-lg font-semibold tracking-tight">Agent Queue</span>
        {paused && (
          <span title="Orchestrator paused" className="ml-auto h-2 w-2 rounded-full bg-amber-400" />
        )}
      </div>
      <nav className="flex-1 space-y-6 overflow-y-auto p-3">
        <div className="space-y-0.5">
          {sections.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                  isActive
                    ? "bg-indigo-500/10 text-indigo-400"
                    : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
                }`
              }
            >
              <Icon className="h-4 w-4 shrink-0" />
              <span className="flex-1 truncate">{label}</span>
            </NavLink>
          ))}
        </div>
        <div>
          <p className="px-3 pb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">
            Projects
          </p>
          <div className="space-y-0.5">
            {(projects ?? []).length === 0 && (
              <p className="px-3 py-1 text-xs text-gray-600">No projects.</p>
            )}
            {(projects ?? []).map((p) => (
              <NavLink
                key={p.id}
                to={`/chat/${p.id}`}
                className={({ isActive }) =>
                  `flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                    isActive
                      ? "bg-indigo-500/10 text-indigo-400"
                      : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
                  }`
                }
              >
                <FolderIcon className="h-4 w-4 shrink-0" />
                <span className="flex-1 truncate">{p.name || p.id}</span>
                {p.paused && <span className="h-1.5 w-1.5 rounded-full bg-amber-400" />}
              </NavLink>
            ))}
          </div>
        </div>
      </nav>
    </aside>
  );
}
```

- [ ] **Step 3: Create `MobileBottomNav`**

```tsx
// dashboard/src/components/nav/MobileBottomNav.tsx
import { NavLink } from "react-router-dom";
import {
  ChatBubbleLeftRightIcon,
  Squares2X2Icon,
  BriefcaseIcon,
  Cog6ToothIcon,
} from "@heroicons/react/24/outline";

const tabs = [
  { to: "/", label: "Chat", icon: ChatBubbleLeftRightIcon, end: true },
  { to: "/command-center", label: "Center", icon: Squares2X2Icon },
  { to: "/work", label: "Work", icon: BriefcaseIcon },
  { to: "/settings", label: "Settings", icon: Cog6ToothIcon },
];

export default function MobileBottomNav() {
  return (
    <nav className="fixed inset-x-0 bottom-0 z-40 flex border-t border-gray-800 bg-gray-900 md:hidden">
      {tabs.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          className={({ isActive }) =>
            `flex flex-1 flex-col items-center gap-0.5 py-2 text-xs ${
              isActive ? "text-indigo-400" : "text-gray-500"
            }`
          }
        >
          <Icon className="h-5 w-5" />
          <span>{label}</span>
        </NavLink>
      ))}
    </nav>
  );
}
```

- [ ] **Step 4: Create `AppShell`**

```tsx
// dashboard/src/components/nav/AppShell.tsx
import { Outlet } from "react-router-dom";
import DesktopSidebar from "./DesktopSidebar";
import MobileBottomNav from "./MobileBottomNav";

export default function AppShell() {
  return (
    <div className="flex h-screen overflow-hidden">
      <DesktopSidebar />
      <main className="flex-1 overflow-y-auto p-4 pb-20 md:p-6 md:pb-6">
        <Outlet />
      </main>
      <MobileBottomNav />
    </div>
  );
}
```

- [ ] **Step 5: Delegate `Layout.tsx` to `AppShell`**

```tsx
// dashboard/src/components/Layout.tsx
import AppShell from "./nav/AppShell";
export default function Layout() {
  return <AppShell />;
}
```

- [ ] **Step 6: Verify build**

Run: `cd dashboard && npm run typecheck && npm run lint`
Expected: exit 0, no new errors.

- [ ] **Step 7: Manual sanity check**

Run (in one terminal, daemon assumed already up on 8091): `cd dashboard && AQ_API_TARGET=http://127.0.0.1:8091 npm run dev`
Visit `http://localhost:5173/system` (still the current index — Task 3 changes routes). Confirm the new sidebar shows Chat / Command Center / Work / Settings on desktop (>=768px). Resize to 390px; confirm bottom-nav appears and sidebar hides.

- [ ] **Step 8: Commit**

```bash
git add dashboard/src/hooks/useMediaQuery.ts dashboard/src/components/nav/ dashboard/src/components/Layout.tsx
git commit -m "feat(dashboard): four-section app shell with desktop sidebar + mobile bottom-nav"
```

---

## Task 2: Chat transcript hook and inline event card

**Files:**
- Create: `dashboard/src/pages/chat/useChatTranscript.ts`
- Create: `dashboard/src/pages/chat/InlineEventCard.tsx`

**Interfaces:**
- Consumes:
  - `fetchChatMessages` from `../../api/chat` (already accepts `threadId`).
  - `sendChatMessage` from `../../api/chat` — the plan extends the helper in Task 2 Step 1 to accept a `threadId` argument.
  - `useEventStream` from `../../ws/useEventStream` — subscribed via `onEvent` callback.
  - Types: `MessageModel` from `../../api/client`, `NotifyEvent` / `MessageSentEvent` / `TaskStartedEvent` / `TaskCompletedEvent` / `GateCreatedEvent` / `GateResolvedEvent` / `PlaybookRunFailedEvent` from `../../ws/types`.
- Produces:
  - `useChatTranscript(projectId: string): { items: TranscriptItem[]; isLoading: boolean; error: unknown; send: (body: string) => Promise<void>; isSending: boolean; sendError: unknown; }`
  - `type TranscriptItem = { kind: "message"; msg: MessageModel & { pending?: boolean; failed?: boolean } } | { kind: "event"; event: NotifyEvent; ts: number }`
  - `threadIdFor(projectId: string): string` returning `"dashboard:" + projectId`
  - `<InlineEventCard event={NotifyEvent} />` — renders a one-line compact card with icon + text + `<Link>` to the relevant detail route when applicable.

- [ ] **Step 1: Extend `sendChatMessage` in `api/chat.ts` to accept a `threadId`**

Open `dashboard/src/api/chat.ts` and add a `threadId` parameter to `sendChatMessage`. Replace the function body:

```typescript
export async function sendChatMessage(
  projectId: string,
  body: string,
  opts: { from?: string; threadId?: string } = {},
): Promise<SendChatMessageResponse> {
  const url = `${baseUrl()}/api/sessions/${encodeURIComponent(
    supervisorName(projectId),
  )}/message`;
  const resp = await throwing(
    await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        body,
        from: opts.from ?? "dashboard",
        from_kind: "user",
        thread_id: opts.threadId,
      }),
    }),
  );
  return (await resp.json()) as SendChatMessageResponse;
}
```

Also update the existing `useSendChatMessage` hook in `dashboard/src/api/hooks.ts` — replace its `mutationFn` body:

```typescript
    mutationFn: (body: string) =>
      sendChatMessage(projectId, body, { threadId: `dashboard:${projectId}` }),
```

(This keeps `ProjectChat.tsx` compiling; the new chat page uses `useChatTranscript` instead.)

- [ ] **Step 2: Create `threadIdFor` + `useChatTranscript` hook**

```typescript
// dashboard/src/pages/chat/useChatTranscript.ts
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchChatMessages, sendChatMessage } from "../../api/chat";
import type { MessageModel } from "../../api/client";
import { useEventStream } from "../../ws/useEventStream";
import type { NotifyEvent } from "../../ws/types";

export function threadIdFor(projectId: string): string {
  return `dashboard:${projectId}`;
}

const EVENT_TYPES_IN_CHAT = new Set<string>([
  "notify.task_started",
  "notify.task_completed",
  "notify.task_failed",
  "gate.created",
  "gate.resolved",
  "notify.playbook_run_failed",
]);

export type PendingMessage = MessageModel & { pending?: boolean; failed?: boolean };

export type TranscriptItem =
  | { kind: "message"; ts: number; msg: PendingMessage }
  | { kind: "event"; ts: number; event: NotifyEvent };

interface HydrateResponse {
  count: number;
  messages: MessageModel[];
}

export function useChatTranscript(projectId: string) {
  const qc = useQueryClient();
  const thread = threadIdFor(projectId);

  const hydrate = useQuery({
    queryKey: ["chat", "thread", projectId, thread],
    queryFn: (): Promise<HydrateResponse> =>
      fetchChatMessages(projectId, { threadId: thread, limit: 200 }),
    enabled: !!projectId,
    staleTime: 15_000,
  });

  const [live, setLive] = useState<MessageModel[]>([]);
  const [pending, setPending] = useState<PendingMessage[]>([]);
  const [events, setEvents] = useState<Array<{ ts: number; event: NotifyEvent }>>([]);
  const seenIds = useRef<Set<string>>(new Set());

  // Reset per-project when projectId changes
  useEffect(() => {
    setLive([]);
    setPending([]);
    setEvents([]);
    seenIds.current = new Set();
  }, [projectId]);

  useEventStream({
    onEvent: useCallback(
      (event: NotifyEvent) => {
        const evProjectId = (event as { project_id?: string | null }).project_id;
        if (evProjectId && evProjectId !== projectId) return;

        if (event.event_type === "message.sent") {
          const evThread = (event as { thread_id?: string }).thread_id;
          if (evThread !== thread) return;
          const msgId = (event as { message_id: string }).message_id;
          if (seenIds.current.has(msgId)) return;
          seenIds.current.add(msgId);
          // Re-fetch to pull the full row (message.sent event doesn't carry body).
          qc.invalidateQueries({ queryKey: ["chat", "thread", projectId, thread] });
          return;
        }

        if (EVENT_TYPES_IN_CHAT.has(event.event_type)) {
          setEvents((prev) => [...prev.slice(-99), { ts: Date.now() / 1000, event }]);
        }
      },
      [projectId, thread, qc],
    ),
  });

  // Hydrated messages drive live; drop pending rows whose body matches a hydrated row.
  useEffect(() => {
    const rows = hydrate.data?.messages ?? [];
    setLive(rows);
    for (const r of rows) seenIds.current.add(r.id);
    setPending((prev) =>
      prev.filter((p) => !rows.some((r) => r.body === p.body && r.from_kind === "user")),
    );
  }, [hydrate.data]);

  const [sendError, setSendError] = useState<unknown>(null);
  const [isSending, setIsSending] = useState(false);

  const send = useCallback(
    async (body: string) => {
      const trimmed = body.trim();
      if (!trimmed) return;
      const optimistic: PendingMessage = {
        id: `optimistic-${Date.now()}`,
        project_id: projectId,
        from_kind: "user",
        from_id: "dashboard",
        to_kind: "session",
        to_id: `supervisor-${projectId}`,
        thread_id: thread,
        body: trimmed,
        priority: 100,
        created_at: Date.now() / 1000,
        delivered_at: null,
        read_at: null,
        subject: null,
        archive_after_inject: false,
        archived_at: null,
        reply_to_id: null,
        via: null,
        pending: true,
      } as PendingMessage;
      setPending((prev) => [...prev, optimistic]);
      setIsSending(true);
      setSendError(null);
      try {
        await sendChatMessage(projectId, trimmed, { threadId: thread });
        qc.invalidateQueries({ queryKey: ["chat", "thread", projectId, thread] });
      } catch (err) {
        setSendError(err);
        setPending((prev) =>
          prev.map((p) => (p.id === optimistic.id ? { ...p, failed: true, pending: false } : p)),
        );
      } finally {
        setIsSending(false);
      }
    },
    [projectId, thread, qc],
  );

  const items = useMemo<TranscriptItem[]>(() => {
    const msgItems: TranscriptItem[] = [
      ...live.map((m) => ({ kind: "message" as const, ts: m.created_at ?? 0, msg: m as PendingMessage })),
      ...pending.map((m) => ({ kind: "message" as const, ts: m.created_at ?? 0, msg: m })),
    ];
    const evItems: TranscriptItem[] = events.map((e) => ({ kind: "event", ts: e.ts, event: e.event }));
    return [...msgItems, ...evItems].sort((a, b) => a.ts - b.ts);
  }, [live, pending, events]);

  return {
    items,
    isLoading: hydrate.isLoading,
    error: hydrate.error,
    send,
    isSending,
    sendError,
  };
}
```

- [ ] **Step 3: Create `InlineEventCard`**

```tsx
// dashboard/src/pages/chat/InlineEventCard.tsx
import { Link } from "react-router-dom";
import {
  PlayCircleIcon,
  CheckCircleIcon,
  XCircleIcon,
  LockClosedIcon,
  LockOpenIcon,
  ExclamationTriangleIcon,
} from "@heroicons/react/24/outline";
import type { NotifyEvent } from "../../ws/types";

function fmt(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString();
}

export default function InlineEventCard({ event, ts }: { event: NotifyEvent; ts: number }) {
  const time = fmt(ts);
  switch (event.event_type) {
    case "notify.task_started":
      return (
        <Row icon={<PlayCircleIcon className="h-4 w-4 text-blue-400" />} time={time}>
          Task started:{" "}
          <Link className="text-indigo-400 hover:underline" to={`/tasks/${event.task.id}`}>
            {event.task.title}
          </Link>
        </Row>
      );
    case "notify.task_completed":
      return (
        <Row icon={<CheckCircleIcon className="h-4 w-4 text-emerald-400" />} time={time}>
          Completed:{" "}
          <Link className="text-indigo-400 hover:underline" to={`/tasks/${event.task.id}`}>
            {event.task.title}
          </Link>
        </Row>
      );
    case "notify.task_failed":
      return (
        <Row icon={<XCircleIcon className="h-4 w-4 text-red-400" />} time={time}>
          Failed:{" "}
          <Link className="text-indigo-400 hover:underline" to={`/tasks/${event.task.id}`}>
            {event.task.title}
          </Link>
          <span className="text-gray-500"> — {event.error_label}</span>
        </Row>
      );
    case "gate.created":
      return (
        <Row icon={<LockClosedIcon className="h-4 w-4 text-amber-400" />} time={time}>
          Gate: {event.gate_type} — {event.title}
        </Row>
      );
    case "gate.resolved":
      return (
        <Row icon={<LockOpenIcon className="h-4 w-4 text-emerald-400" />} time={time}>
          Gate resolved by {event.resolved_by}
        </Row>
      );
    case "notify.playbook_run_failed":
      return (
        <Row icon={<ExclamationTriangleIcon className="h-4 w-4 text-red-400" />} time={time}>
          Playbook{" "}
          <Link className="text-indigo-400 hover:underline" to={`/playbooks/${event.playbook_id}`}>
            {event.playbook_id}
          </Link>{" "}
          failed at {event.failed_at_node}
        </Row>
      );
    default:
      return null;
  }
}

function Row({
  icon,
  time,
  children,
}: {
  icon: React.ReactNode;
  time: string;
  children: React.ReactNode;
}) {
  return (
    <div className="my-1 flex items-center gap-2 rounded border border-gray-800/60 bg-gray-900/40 px-2 py-1 text-xs text-gray-300">
      {icon}
      <span className="flex-1">{children}</span>
      <span className="text-gray-600">{time}</span>
    </div>
  );
}
```

- [ ] **Step 4: Verify build**

Run: `cd dashboard && npm run typecheck && npm run lint`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/pages/chat/useChatTranscript.ts dashboard/src/pages/chat/InlineEventCard.tsx dashboard/src/api/chat.ts dashboard/src/api/hooks.ts
git commit -m "feat(dashboard): chat transcript hook with WS live-refresh + inline event cards"
```

---

## Task 3: Chat landing and per-project conversation pages + new route tree

**Files:**
- Create: `dashboard/src/pages/chat/ChatLanding.tsx`
- Create: `dashboard/src/pages/chat/ChatConversation.tsx`
- Create: `dashboard/src/pages/command-center/CommandCenterPlaceholder.tsx`
- Modify: `dashboard/src/App.tsx`

**Interfaces:**
- Consumes: `useProjects` (from `../../api/hooks`), `useChatTranscript`, `InlineEventCard`, `threadIdFor`.
- Produces:
  - Route `/` → `<ChatLanding />` (project picker; single-project auto-forwards to `/chat/:id`).
  - Route `/chat/:projectId` → `<ChatConversation />`.
  - Route `/command-center` → `<CommandCenterPlaceholder />`.

**Design decisions (explicit for reviewers):**
- Per-project chat via route param `/chat/:projectId`. Rationale: the wire is scoped to `supervisor-<pid>`; a URL-addressable per-project conversation makes deep-links from event cards trivial, matches the sidebar project list, and avoids a global chat picker that has to pretend a single conversation exists.
- History hydration uses the existing `GET /api/sessions/supervisor-<pid>/messages` endpoint with `thread_id` filter (backed by the `message_list` command in `src/commands/message_commands.py`). No new backend surface required.
- `ChatLanding` shows a project grid, not an "all projects" merged view — the underlying message queue is per-project and merging them without a clear ownership model would confuse the transcript.

- [ ] **Step 1: Create `ChatConversation`**

```tsx
// dashboard/src/pages/chat/ChatConversation.tsx
import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { PaperAirplaneIcon } from "@heroicons/react/24/outline";
import { useChatTranscript } from "./useChatTranscript";
import InlineEventCard from "./InlineEventCard";
import type { PendingMessage } from "./useChatTranscript";

function fmt(ts: number | null | undefined): string {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString();
}

function Bubble({ msg }: { msg: PendingMessage }) {
  const mine = msg.from_kind === "user";
  return (
    <div className={`flex ${mine ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[85%] rounded-lg px-3 py-2 text-sm ${
          mine
            ? msg.failed
              ? "bg-red-600/20 text-red-100"
              : "bg-indigo-600/20 text-indigo-100"
            : "bg-gray-800 text-gray-100"
        } ${msg.pending ? "opacity-60" : ""}`}
      >
        <div className="mb-1 flex items-center gap-2 text-xs text-gray-400">
          <span className="font-mono">{`${msg.from_kind}:${msg.from_id}`}</span>
          <span>{fmt(msg.created_at)}</span>
          {msg.pending && <span className="text-gray-500">sending…</span>}
          {msg.failed && <span className="text-red-400">failed</span>}
        </div>
        <div className="whitespace-pre-wrap">{msg.body}</div>
      </div>
    </div>
  );
}

export default function ChatConversation() {
  const { projectId = "" } = useParams();
  const { items, isLoading, error, send, isSending, sendError } = useChatTranscript(projectId);
  const [body, setBody] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [items]);

  const submit = () => {
    if (!body.trim() || isSending) return;
    void send(body).then(() => setBody(""));
  };

  return (
    <div className="flex h-[calc(100vh-8rem)] flex-col space-y-3 md:h-[calc(100vh-4rem)]">
      <header className="hidden md:block">
        <h2 className="text-lg font-semibold">Chat with supervisor</h2>
        <p className="text-xs text-gray-500">
          Talking to <span className="font-mono">supervisor-{projectId}</span>.
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
        {!isLoading && items.length === 0 && (
          <p className="text-sm text-gray-500">No messages yet — say hello.</p>
        )}
        {items.map((it, idx) =>
          it.kind === "message" ? (
            <Bubble key={`m-${it.msg.id}-${idx}`} msg={it.msg} />
          ) : (
            <InlineEventCard key={`e-${idx}`} event={it.event} ts={it.ts} />
          ),
        )}
      </div>

      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              submit();
            }
          }}
          rows={2}
          placeholder="Message the supervisor (Cmd/Ctrl+Enter to send)…"
          className="flex-1 resize-none rounded border border-gray-800 bg-gray-900 px-2 py-1 text-sm text-gray-200"
        />
        <button
          type="submit"
          disabled={isSending || !body.trim()}
          className="inline-flex items-center gap-1 rounded bg-indigo-600 px-3 py-1 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
        >
          <PaperAirplaneIcon className="h-4 w-4" />
          <span className="hidden sm:inline">Send</span>
        </button>
      </form>
      {sendError !== null && (
        <p className="text-xs text-red-400">{(sendError as Error).message}</p>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Create `ChatLanding`**

```tsx
// dashboard/src/pages/chat/ChatLanding.tsx
import { useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ChatBubbleLeftRightIcon } from "@heroicons/react/24/outline";
import { useProjects } from "../../api/hooks";

export default function ChatLanding() {
  const { data: projects, isLoading } = useProjects();
  const navigate = useNavigate();
  const list = projects ?? [];

  // Auto-forward when there's exactly one project — a single-project user
  // never wants to pick from a list of one.
  useEffect(() => {
    if (!isLoading && list.length === 1) {
      navigate(`/chat/${list[0].id}`, { replace: true });
    }
  }, [isLoading, list, navigate]);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">Chat</h1>
        <p className="text-sm text-gray-500">
          Pick a project to talk to its supervisor.
        </p>
      </header>
      {isLoading && <p className="text-sm text-gray-500">Loading projects…</p>}
      {!isLoading && list.length === 0 && (
        <p className="text-sm text-gray-500">
          No projects yet. Create one in{" "}
          <Link to="/settings/config" className="text-indigo-400 hover:underline">
            Settings
          </Link>
          .
        </p>
      )}
      <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {list.map((p) => (
          <li key={p.id}>
            <Link
              to={`/chat/${p.id}`}
              className="flex items-center gap-3 rounded border border-gray-800 bg-gray-900 p-4 hover:border-indigo-500/50 hover:bg-gray-800"
            >
              <ChatBubbleLeftRightIcon className="h-6 w-6 text-indigo-400" />
              <div>
                <p className="font-medium text-gray-200">{p.name || p.id}</p>
                <p className="font-mono text-xs text-gray-500">supervisor-{p.id}</p>
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
```

- [ ] **Step 3: Create `CommandCenterPlaceholder`**

```tsx
// dashboard/src/pages/command-center/CommandCenterPlaceholder.tsx
import { Link } from "react-router-dom";
import { Squares2X2Icon } from "@heroicons/react/24/outline";

export default function CommandCenterPlaceholder() {
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">Command Center</h1>
        <p className="text-sm text-gray-500">
          Live pan/zoom work-graph canvas — Phase 4.
        </p>
      </header>
      <div className="flex flex-col items-center justify-center gap-3 rounded border border-dashed border-gray-700 bg-gray-900/40 p-12 text-center">
        <Squares2X2Icon className="h-10 w-10 text-gray-600" />
        <p className="text-gray-400">Coming in Phase 4.</p>
        <p className="text-sm text-gray-500">
          Until then, use{" "}
          <Link to="/" className="text-indigo-400 hover:underline">
            Chat
          </Link>{" "}
          to talk to the supervisor or{" "}
          <Link to="/work" className="text-indigo-400 hover:underline">
            Work
          </Link>{" "}
          to see tasks and agents.
        </p>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Rewrite `App.tsx` route tree**

```tsx
// dashboard/src/App.tsx
import { Routes, Route, Navigate } from "react-router-dom";
import Layout from "./components/Layout";

import ChatLanding from "./pages/chat/ChatLanding";
import ChatConversation from "./pages/chat/ChatConversation";
import CommandCenterPlaceholder from "./pages/command-center/CommandCenterPlaceholder";

import WorkIndex from "./pages/work/WorkIndex";

import SettingsLayout from "./pages/settings/SettingsLayout";
import SystemPlaybooks from "./pages/system/Playbooks";
import SystemProfiles from "./pages/system/Profiles";
import SystemConfig from "./pages/system/Config";
import IntelligenceClassesStub from "./pages/settings/IntelligenceClassesStub";

import SystemOverview from "./pages/system/Overview";
import SystemEvents from "./pages/system/Events";
import SystemSessions from "./pages/system/Sessions";
import SystemGates from "./pages/system/Gates";

import ProjectLayout from "./pages/project/ProjectLayout";
import ProjectOverview from "./pages/project/Overview";
import ProjectTasks from "./pages/project/Tasks";
import ProjectWorkspaces from "./pages/project/Workspaces";
import ProjectProfiles from "./pages/project/Profiles";
import ProjectPlaybooks from "./pages/project/Playbooks";
import ProjectConfig from "./pages/project/Config";
import ProjectSessions from "./pages/project/Sessions";
import ProjectChat from "./pages/project/Chat";

import TaskDetail from "./pages/TaskDetail";
import PlaybookDetail from "./pages/PlaybookDetail";
import SessionDetail from "./pages/SessionDetail";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        {/* Top-level IA — Phase 3 */}
        <Route index element={<ChatLanding />} />
        <Route path="chat/:projectId" element={<ChatConversation />} />
        <Route path="command-center" element={<CommandCenterPlaceholder />} />
        <Route path="work" element={<WorkIndex />} />

        {/* Settings hub — playbooks / profiles / intelligence-classes / config */}
        <Route path="settings" element={<SettingsLayout />}>
          <Route index element={<Navigate to="playbooks" replace />} />
          <Route path="playbooks" element={<SystemPlaybooks />} />
          <Route path="profiles" element={<SystemProfiles />} />
          <Route path="intelligence-classes" element={<IntelligenceClassesStub />} />
          <Route path="config" element={<SystemConfig />} />
        </Route>

        {/* Existing sub-surfaces kept reachable (Events/Sessions/Gates as sub-tabs of Work) */}
        <Route path="work/events" element={<SystemEvents />} />
        <Route path="work/sessions" element={<SystemSessions />} />
        <Route path="work/gates" element={<SystemGates />} />

        {/* Project surfaces stay intact for deep-links */}
        <Route path="projects/:projectId" element={<ProjectLayout />}>
          <Route index element={<ProjectOverview />} />
          <Route path="tasks" element={<ProjectTasks />} />
          <Route path="sessions" element={<ProjectSessions />} />
          <Route path="chat" element={<ProjectChat />} />
          <Route path="workspaces" element={<ProjectWorkspaces />} />
          <Route path="profiles" element={<ProjectProfiles />} />
          <Route path="playbooks" element={<ProjectPlaybooks />} />
          <Route path="config" element={<ProjectConfig />} />
        </Route>

        {/* Detail routes unchanged */}
        <Route path="tasks/:taskId" element={<TaskDetail />} />
        <Route path="sessions/:sessionId" element={<SessionDetail />} />
        <Route path="playbooks/:playbookId" element={<PlaybookDetail />} />

        {/* Legacy redirects — the four Phase-2 nav entries + old aliases */}
        <Route path="system" element={<Navigate to="/work" replace />} />
        <Route path="system/events" element={<Navigate to="/work/events" replace />} />
        <Route path="system/sessions" element={<Navigate to="/work/sessions" replace />} />
        <Route path="system/gates" element={<Navigate to="/work/gates" replace />} />
        <Route path="system/playbooks" element={<Navigate to="/settings/playbooks" replace />} />
        <Route path="system/profiles" element={<Navigate to="/settings/profiles" replace />} />
        <Route path="system/config" element={<Navigate to="/settings/config" replace />} />
        <Route path="agents" element={<Navigate to="/work" replace />} />
        <Route path="tasks" element={<Navigate to="/work" replace />} />
        <Route path="playbooks" element={<Navigate to="/settings/playbooks" replace />} />
        <Route path="events" element={<Navigate to="/work/events" replace />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
```

`SystemOverview`, `IntelligenceClassesStub`, `SettingsLayout`, `WorkIndex` are stubs / new files that arrive in Tasks 4–5. Import them here anyway; Vite dev server will error until those files exist, so complete the following two tasks before starting the dev server. Alternatively, comment the offending imports until Task 4/5 finish — but the plan is small enough to just proceed straight through.

- [ ] **Step 5: Commit (partial — do not verify build until Tasks 4/5 land)**

```bash
git add dashboard/src/pages/chat/ dashboard/src/pages/command-center/ dashboard/src/App.tsx
git commit -m "feat(dashboard): chat landing + per-project conversation + new route tree"
```

---

## Task 4: Settings hub + stubs

**Files:**
- Create: `dashboard/src/components/nav/SettingsSidebar.tsx`
- Create: `dashboard/src/pages/settings/SettingsLayout.tsx`
- Create: `dashboard/src/pages/settings/IntelligenceClassesStub.tsx`

**Interfaces:**
- Consumes: existing `pages/system/Playbooks.tsx`, `pages/system/Profiles.tsx`, `pages/system/Config.tsx` (unchanged; rendered inside the settings outlet).
- Produces:
  - `<SettingsLayout />` — renders `SettingsSidebar` + `<Outlet />`.
  - `<SettingsSidebar />` — secondary nav with 4 links (Playbooks, Profiles, Intelligence Classes, Config).
  - `<IntelligenceClassesStub />` — placeholder page.

- [ ] **Step 1: Create `SettingsSidebar`**

```tsx
// dashboard/src/components/nav/SettingsSidebar.tsx
import { NavLink } from "react-router-dom";
import {
  BookOpenIcon,
  UserGroupIcon,
  CpuChipIcon,
  Cog6ToothIcon,
} from "@heroicons/react/24/outline";

const links = [
  { to: "playbooks", label: "Playbooks", icon: BookOpenIcon },
  { to: "profiles", label: "Profiles", icon: UserGroupIcon },
  { to: "intelligence-classes", label: "Intelligence Classes", icon: CpuChipIcon },
  { to: "config", label: "Config", icon: Cog6ToothIcon },
];

export default function SettingsSidebar() {
  return (
    <aside className="flex shrink-0 gap-1 overflow-x-auto border-b border-gray-800 pb-2 md:w-56 md:flex-col md:border-b-0 md:border-r md:pr-4">
      {links.map(({ to, label, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          className={({ isActive }) =>
            `flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium whitespace-nowrap ${
              isActive
                ? "bg-indigo-500/10 text-indigo-400"
                : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
            }`
          }
        >
          <Icon className="h-4 w-4 shrink-0" />
          <span>{label}</span>
        </NavLink>
      ))}
    </aside>
  );
}
```

- [ ] **Step 2: Create `SettingsLayout`**

```tsx
// dashboard/src/pages/settings/SettingsLayout.tsx
import { Outlet } from "react-router-dom";
import SettingsSidebar from "../../components/nav/SettingsSidebar";

export default function SettingsLayout() {
  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-bold">Settings</h1>
        <p className="text-sm text-gray-500">
          Curation surfaces — everything you tell the system how to behave.
        </p>
      </header>
      <div className="flex flex-col gap-6 md:flex-row">
        <SettingsSidebar />
        <div className="min-w-0 flex-1">
          <Outlet />
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Create `IntelligenceClassesStub`**

```tsx
// dashboard/src/pages/settings/IntelligenceClassesStub.tsx
import { CpuChipIcon } from "@heroicons/react/24/outline";

export default function IntelligenceClassesStub() {
  return (
    <div className="space-y-4">
      <header>
        <h2 className="text-lg font-semibold">Intelligence Classes</h2>
        <p className="text-sm text-gray-500">
          Curated model tiers referenced by task routing. Sourced from{" "}
          <code className="rounded bg-gray-800 px-1 text-xs">
            vault/intelligence-classes/*.md
          </code>
          .
        </p>
      </header>
      <div className="flex flex-col items-center justify-center gap-3 rounded border border-dashed border-gray-700 bg-gray-900/40 p-10 text-center">
        <CpuChipIcon className="h-8 w-8 text-gray-600" />
        <p className="text-gray-400">Wiring lands with Phase 1 (control plane core).</p>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/components/nav/SettingsSidebar.tsx dashboard/src/pages/settings/
git commit -m "feat(dashboard): settings hub with playbooks/profiles/classes/config sub-nav"
```

---

## Task 5: Work index (combined tasks + agents view)

**Files:**
- Create: `dashboard/src/pages/work/WorkIndex.tsx`
- Create: `dashboard/src/pages/work/WorkTasks.tsx`
- Create: `dashboard/src/pages/work/WorkAgents.tsx`

**Interfaces:**
- Consumes:
  - `useActiveTasksAllProjects`, `useAllAgents`, `useProjects` from `../../api/hooks` (already used by `pages/system/Overview.tsx`).
  - Types: `Task`, `Agent` from `../../api/hooks`.
- Produces:
  - `<WorkIndex />` — filter bar (project select, status multi-toggle, "show completed" switch) + `<WorkTasks />` + `<WorkAgents />` stacked.
  - `<WorkTasks projectId? statusFilter? showCompleted />` — table with columns Title / Project / Status / Priority / Agent.
  - `<WorkAgents projectId? />` — table with columns Name / State / Project / Current Task.

- [ ] **Step 1: Create `WorkTasks`**

```tsx
// dashboard/src/pages/work/WorkTasks.tsx
import { Link } from "react-router-dom";
import { useActiveTasksAllProjects } from "../../api/hooks";

interface Props {
  projectId?: string;
  statusFilter?: Set<string>;
  showCompleted: boolean;
}

export default function WorkTasks({ projectId, statusFilter, showCompleted }: Props) {
  const { data: tasks = [], isLoading } = useActiveTasksAllProjects();

  const filtered = tasks.filter((t) => {
    if (projectId && t.project_id !== projectId) return false;
    if (statusFilter && statusFilter.size > 0 && !statusFilter.has((t.status ?? "").toUpperCase()))
      return false;
    if (!showCompleted && ["COMPLETED", "CANCELED"].includes((t.status ?? "").toUpperCase()))
      return false;
    return true;
  });

  return (
    <section className="space-y-2">
      <h2 className="text-lg font-semibold">Tasks ({filtered.length})</h2>
      <div className="overflow-x-auto rounded border border-gray-800">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-900 text-xs uppercase text-gray-500">
            <tr>
              <th className="px-3 py-2">Title</th>
              <th className="px-3 py-2">Project</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Priority</th>
              <th className="px-3 py-2">Agent</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800">
            {isLoading && (
              <tr>
                <td colSpan={5} className="px-3 py-4 text-gray-500">
                  Loading…
                </td>
              </tr>
            )}
            {!isLoading && filtered.length === 0 && (
              <tr>
                <td colSpan={5} className="px-3 py-4 text-center text-gray-500">
                  No matching tasks.
                </td>
              </tr>
            )}
            {filtered.map((t) => (
              <tr key={t.id} className="hover:bg-gray-900/50">
                <td className="px-3 py-2">
                  <Link to={`/tasks/${t.id}`} className="text-indigo-400 hover:underline">
                    {t.title}
                  </Link>
                </td>
                <td className="px-3 py-2 text-gray-400">{t.project_id}</td>
                <td className="px-3 py-2 text-gray-300">{t.status}</td>
                <td className="px-3 py-2 text-gray-400">{t.priority ?? "-"}</td>
                <td className="px-3 py-2 text-gray-400">{t.assigned_agent ?? "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
```

- [ ] **Step 2: Create `WorkAgents`**

```tsx
// dashboard/src/pages/work/WorkAgents.tsx
import { useAllAgents, useProjects } from "../../api/hooks";

interface Props {
  projectId?: string;
}

export default function WorkAgents({ projectId }: Props) {
  const { data: projects } = useProjects();
  const ids = (projects ?? []).map((p) => p.id);
  const { data: agents = [], isLoading } = useAllAgents(ids);

  const filtered = projectId ? agents.filter((a) => a.project_id === projectId) : agents;

  return (
    <section className="space-y-2">
      <h2 className="text-lg font-semibold">Agents ({filtered.length})</h2>
      <div className="overflow-x-auto rounded border border-gray-800">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-900 text-xs uppercase text-gray-500">
            <tr>
              <th className="px-3 py-2">Name</th>
              <th className="px-3 py-2">State</th>
              <th className="px-3 py-2">Project</th>
              <th className="px-3 py-2">Task</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800">
            {isLoading && (
              <tr>
                <td colSpan={4} className="px-3 py-4 text-gray-500">
                  Loading…
                </td>
              </tr>
            )}
            {!isLoading && filtered.length === 0 && (
              <tr>
                <td colSpan={4} className="px-3 py-4 text-center text-gray-500">
                  No agents.
                </td>
              </tr>
            )}
            {filtered.map((a) => (
              <tr key={`${a.project_id}:${a.name}`} className="hover:bg-gray-900/50">
                <td className="px-3 py-2 text-gray-200">{a.name}</td>
                <td className="px-3 py-2 text-gray-300">{a.state}</td>
                <td className="px-3 py-2 text-gray-400">{a.project_id}</td>
                <td className="px-3 py-2 text-gray-400">{a.current_task_id ?? "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
```

Note: the exact field names on `Agent` (e.g. `state`, `current_task_id`, `project_id`) are what the existing `pages/system/Overview.tsx` uses when it renders agent rows. If the current codebase uses different names (e.g. `busy_task_id`), match those instead — verify by opening `pages/system/Overview.tsx` before pasting.

- [ ] **Step 3: Create `WorkIndex` (filter bar + tables)**

```tsx
// dashboard/src/pages/work/WorkIndex.tsx
import { useState } from "react";
import { useProjects } from "../../api/hooks";
import WorkTasks from "./WorkTasks";
import WorkAgents from "./WorkAgents";

const STATUSES = [
  "PENDING",
  "READY",
  "IN_PROGRESS",
  "AWAITING_APPROVAL",
  "AWAITING_PLAN_APPROVAL",
  "WAITING_INPUT",
  "COMPLETED",
  "FAILED",
  "BLOCKED",
  "CANCELED",
];

export default function WorkIndex() {
  const { data: projects } = useProjects();
  const [projectId, setProjectId] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<Set<string>>(new Set());
  const [showCompleted, setShowCompleted] = useState(false);

  const toggleStatus = (s: string) => {
    setStatusFilter((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });
  };

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">Work</h1>
        <p className="text-sm text-gray-500">
          Everything the system is doing or waiting on. Filter to focus.
        </p>
      </header>

      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-xs text-gray-500" htmlFor="proj">
            Project:
          </label>
          <select
            id="proj"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            className="rounded border border-gray-800 bg-gray-900 px-2 py-1 text-sm text-gray-200"
          >
            <option value="">All</option>
            {(projects ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name || p.id}
              </option>
            ))}
          </select>
          <label className="ml-4 flex items-center gap-1 text-xs text-gray-500">
            <input
              type="checkbox"
              checked={showCompleted}
              onChange={(e) => setShowCompleted(e.target.checked)}
            />
            Show completed
          </label>
        </div>
        <div className="flex flex-wrap gap-1">
          {STATUSES.map((s) => (
            <button
              key={s}
              onClick={() => toggleStatus(s)}
              className={`rounded-full px-2 py-0.5 text-xs ${
                statusFilter.has(s)
                  ? "bg-indigo-500/20 text-indigo-300"
                  : "bg-gray-800 text-gray-400 hover:bg-gray-700"
              }`}
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      <WorkTasks
        projectId={projectId || undefined}
        statusFilter={statusFilter}
        showCompleted={showCompleted}
      />
      <WorkAgents projectId={projectId || undefined} />
    </div>
  );
}
```

- [ ] **Step 4: Verify build (this is the first task where every route-tree import exists)**

Run: `cd dashboard && npm run typecheck && npm run lint && npm run build`
Expected: exit 0.

If typecheck fails on `Agent` field access, open `dashboard/src/api/hooks.ts` around line 231 (`useAgents`) and `dashboard/src/pages/system/Overview.tsx` to see the exact field names, then correct `WorkAgents.tsx` inline (this is a code step, not a placeholder — the source of truth is the existing typed `Agent` interface).

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/pages/work/
git commit -m "feat(dashboard): Work section — combined filterable tasks + agents tables"
```

---

## Task 6: Mobile polish + full manual verification

**Files:**
- Modify: `dashboard/src/pages/chat/ChatConversation.tsx` (small mobile-only header)
- Modify: `dashboard/src/pages/chat/ChatLanding.tsx` (grid spacing)

**Interfaces:** none new.

- [ ] **Step 1: Confirm scroll-and-input behavior at 390px**

Start the dev server:

```bash
cd dashboard && AQ_API_TARGET=http://127.0.0.1:8091 npm run dev
```

Open browser dev tools, set viewport to 390×844 (iPhone 14). Visit `/`.
- If exactly one project exists, expect redirect to `/chat/<id>`.
- Otherwise expect a project grid — cards should be single-column at 390px.
- Tap a project — the transcript should fill the viewport minus the bottom-nav; input row should sit above the bottom-nav (not overlap). Bottom-nav must be visible and tappable.

- [ ] **Step 2: Fix any overlap by tightening `ChatConversation` height**

If the input overlaps the mobile bottom-nav, adjust the outer container height in `ChatConversation.tsx` from `h-[calc(100vh-8rem)]` upward (e.g. `h-[calc(100vh-9rem)]`) until the send button sits comfortably above the bottom-nav.

- [ ] **Step 3: Verify desktop (1280×800)**

Set viewport to 1280×800. Confirm:
- Sidebar shows four sections + project list.
- Chat renders wide; message bubbles cap at ~85% width.
- Work page: filter bar wraps naturally, both tables render.
- Settings page: sub-nav is a column on the left; each of Playbooks/Profiles/Intelligence Classes/Config renders in the right pane.
- Command Center shows the "Coming in Phase 4" placeholder.

- [ ] **Step 4: Verify chat wire end-to-end (requires a live supervisor session)**

With a project that has a running `supervisor-<pid>` session:
- Send a short message via the composer. A grey "sending…" bubble appears immediately.
- Once the SDK returns, TanStack Query invalidates and re-fetches; the optimistic bubble is replaced by the persisted row (identical `body`, same-side alignment).
- When the supervisor replies, a `message.sent` WS event with `thread_id="dashboard:<pid>"` triggers another re-fetch; the reply appears in the transcript.
- Trigger a task in the project (e.g. `aq task create ...`); confirm a `notify.task_started` event card appears inline in the transcript.

- [ ] **Step 5: Verify legacy redirects**

Visit each of these and confirm the URL bar changes to the target:
- `/system` → `/work`
- `/system/events` → `/work/events`
- `/system/playbooks` → `/settings/playbooks`
- `/system/config` → `/settings/config`
- `/agents` → `/work`
- `/random-garbage-path` → `/`

- [ ] **Step 6: Final commit**

If Step 2 modified the height, commit; otherwise skip.

```bash
git add dashboard/src/pages/chat/ChatConversation.tsx
git commit -m "chore(dashboard): tune mobile chat viewport height for bottom-nav clearance"
```

---

## Self-Review

**1. Spec coverage (Phase 3 = §9.1 + §12.3):**
- Nav collapse to Chat / Command Center / Work / Settings — Tasks 1, 3, 4, 5.
- Chat landing wired to supervisor sessions — Tasks 2, 3.
- Mobile layouts — Task 1 (bottom-nav), Task 6 (viewport tuning), phone-first chat in Task 3.
- Work tables — Task 5.
- Settings consolidation (playbooks / profiles / classes / config) — Task 4.
- Existing detail routes preserved — Task 3 route tree (`/tasks/:id`, `/sessions/:id`, `/playbooks/:id`, and all `/projects/:pid/*` sub-routes).
- Legacy route redirects — Task 3 App.tsx, verified Task 6 Step 5.

**2. Placeholder scan:** none — every step contains full code or a specific verification action. Task 5 Step 2 note about `Agent` field names is a directed lookup with an exact line reference, not a "TBD".

**3. Type consistency:** `PendingMessage`, `TranscriptItem`, `threadIdFor` defined in Task 2 and imported unchanged in Task 3. `InlineEventCard` prop shape (`event`, `ts`) matches its usage. WS event type imports match `dashboard/src/ws/types.ts` verbatim.

**Deferred to later phases (per spec §12):** Command Center canvas (Phase 4), console pane-view + work preview (Phase 5), spec ingestion (Phase 6). Task 3 leaves an explicit placeholder page for Command Center rather than gating this phase on it.
