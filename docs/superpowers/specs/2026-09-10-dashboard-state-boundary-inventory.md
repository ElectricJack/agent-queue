# Dashboard state-boundary inventory

**Date:** 2026-09-10

**Scope:** production code under `dashboard/src`

**Purpose:** input to the typed dashboard-state contract and the removal of browser-backed
feature state

This inventory classifies the dashboard's persistent state and the client stores that can look
durable. It is intentionally a roll-forward inventory: server state becomes authoritative on first
use. Existing browser values are not imported, reconciled, or retained as fallbacks.

## Classification and namespace rules

| Classification | Meaning | Persistence and synchronization rule |
|---|---|---|
| **Shared workspace** | Organization that every operator of this AQ installation should see | Store once in a workspace namespace; load from the server; publish revisioned updates to every connected dashboard, regardless of user |
| **Per-user roaming** | A user preference that should follow the same authenticated principal to another browser or machine | Store below the user principal; load from the server; publish updates to that user's dashboards only; never fall back to browser feature storage |
| **Device-local transport** | State required to resume or protect one browser's live transport, with no product meaning on another device | Keep local to the browser/connection; never copy into a preference namespace; recover by reconnect/refetch when it is absent or stale |
| **Ephemeral** | Address, derived cache, draft, selection, animation, or open connection state whose lifetime is the route, component, or page | Keep in the URL or memory as identified below; do not create a durable namespace or synchronize it across dashboards |

The namespace names below are inputs to the contract task, not a claim that these API names already
exist. Three namespaces cover all feature state without schema-per-setting churn:

- `workspace.nav_organization`: shared folders, folder collapse, assignments, and project order.
- `user.shell_preferences`: theme, pane/surface presentation, flock collapse, and last project.
- `user.command_center_preferences`: graph presentation, keyed further by project or view where
  identified below.

## Persistent and persistence-worthy feature state

| State item | Owner | Classification and scope | Current storage | Desired server namespace | Mutation sites | Load/live-sync expectation |
|---|---|---|---|---|---|---|
| Project folder definitions (`id`, `name`) | Left rail / `navOrganization` | **Shared workspace**, installation-wide | `localStorage["aq.shell.project-organization"].folders` | `workspace.nav_organization.folders` | `createFolder`, `renameFolder`, `deleteFolder` in `dashboard/src/shell/navOrganization.ts`; handlers in `ProjectTree.tsx` | Server snapshot is authoritative; revisioned mutations fan out to every dashboard |
| Folder collapsed state | Left rail / `navOrganization` | **Shared workspace**, per folder | Same localStorage document | `workspace.nav_organization.folders[].collapsed` | `toggleFolder` in `navOrganization.ts`; folder controls in `ProjectTree.tsx` | Shared exactly like folder definitions; this is part of organization, not the unrelated flock preference |
| Project-to-folder assignments | Left rail / `navOrganization` | **Shared workspace**, project id to folder id | `localStorage["aq.shell.project-organization"].assignments` | `workspace.nav_organization.assignments` | `moveProject` and `deleteFolder` in `navOrganization.ts`; drag/drop and keyboard handlers in `ProjectTree.tsx` | All users converge on the newest accepted revision; unknown/deleted ids remain safely ignored |
| Project rail order and folder order | Left rail / `navOrganization` | **Shared workspace**, installation-wide | `localStorage["aq.shell.project-organization"].order` plus folder array order | `workspace.nav_organization.project_order` and `.folder_order` | `moveProject`, `moveFolder`, `nudgeFolder`, `withProjectsRanked` in `navOrganization.ts`; `ProjectTree.tsx` | Same shared snapshot/event contract; deterministic stale-revision handling is required for concurrent reorder |
| Theme | Shell | **Per-user roaming**, global | No state today: the shell is fixed dark (`dashboard/src/index.css` and dark color classes) | `user.shell_preferences.theme` | None today; the future theme control/reset owns writes | Server default is `dark`; adding a selector must use this namespace rather than browser or OS-only persistence |
| Shell-pane width by pane view | Pane shell | **Per-user roaming**, keyed by pane manifest id | Dynamic `localStorage["aq:shellpane:width:<viewId>"]` | `user.shell_preferences.pane_widths[view_id]` | `loadWidth`/`persistWidth` and `ShellPaneProvider.setWidth` in `dashboard/src/panes/store.tsx` | Load the user's map before opening a pane; width changes synchronize to the same user's dashboards |
| Right-surface width | Shell right surface | **Per-user roaming**, global | `localStorage["aq:rightsurface:width"]` | `user.shell_preferences.right_surface.width` | `loadWidth` and `RightSurfaceProvider.setWidth` in `dashboard/src/shell/useRightSurface.tsx` | Server default `480`; revisioned write on resize; same-user live update |
| Right-surface kind and activity tab | Shell right surface | **Per-user roaming**, global | React state only: `kind` and `activityTab` in `RightSurfaceProvider` | `user.shell_preferences.right_surface.kind` and `.activity_tab` | `setKind`/`setActivityTab` in `useRightSurface.tsx`; shortcuts and `?openDrawer=` bridge in `AppShellV2.tsx` | Restore the user's last surface/tab after the server load; a one-shot URL command may override once, then is removed |
| Open shell pane view and validated args | Pane shell | **Per-user roaming**, global, discriminated by registered pane id | `stateRef` external store in `ShellPaneProvider`; resets on reload | `user.shell_preferences.right_surface.pane` | `open`, `close`, and `setArgs` in `dashboard/src/panes/store.tsx`; callers use `useShellPaneStore` | Store only manifest-validated `view`/`args`; synchronize with the other right-surface fields. Toolbar callbacks and pane component internals remain ephemeral |
| Projects-section disclosure | Left rail | **Per-user roaming**, global | `projectsOpen` React state in `dashboard/src/shell/LeftRail.tsx` | `user.shell_preferences.projects_section_open` | Projects disclosure button and the add-folder/project-success paths call `setProjectsOpen` | Server default `true`; same-user live update; this controls the entire Projects section and is separate from shared individual-folder collapse |
| Agent-flock collapse | Left rail / agent flock | **Per-user roaming**, global | `localStorage["aq:flock:collapsed"]` | `user.shell_preferences.agent_flock_collapsed` | lazy read and `toggle` in `dashboard/src/shell/AgentFlock.tsx` | Server default `false`; same-user dashboards update live; unrelated to shared project-folder collapse |
| Last selected project | Router / command-center redirect | **Per-user roaming**, global | `localStorage["aq.dashboard.lastProjectId"]` | `user.shell_preferences.last_project_id` | `ProjectScopePaneSync` writes and `CommandCenterRedirect` reads in `dashboard/src/App.tsx` | Server value chooses the redirect when the project still exists; otherwise use the first project and repair/reset the preference |
| Graph density | Command-center layout | **Per-user roaming**, global command-center presentation | `localStorage["aq.command-center.graph-density"]` | `user.command_center_preferences.global.density` | `storedDensity` in `layout-v2/density.ts`; `setDensity` and persistence effect in `LayoutCanvas.tsx` | Server default `comfortable`; same-user live update; server world coordinates are unaffected |
| Expanded graph hierarchy, including the finished subset | Command-center hierarchy | **Per-user roaming**, keyed by project (shared by canvas, mobile list, toolbar, and graph variants) | `localStorage["aq:command-center:expanded-task-ids:v1"]`, `localStorage["aq:command-center:expanded-finished-task-ids:v1"]`, and module-level sets/listeners | `user.command_center_preferences.projects[project_id].expanded_task_ids` with finished metadata | `setExpandedTaskIds`/`toggleExpandedId` in `dashboard/src/pages/command-center/useGraphHierarchy.ts`; task/container toggles call the hook | Server values are authoritative per project; updates fan out to the same user's dashboards. Filter-forced expansion stays server-derived and must not be persisted |
| Manual graph positions | Command-center layout | **Per-user roaming**, keyed by project; playbook cards use a separate `__playbooks__` view scope | `localStorage["aq.command-center.graph-positions"]`; mirrored in `LayoutCanvas.manualPositions` | `user.command_center_preferences.projects[project_id].manual_positions` and `.views.playbooks.manual_positions` | `saveGraphPosition`/`clearGraphPositions` in `layout-v2/manualPositions.ts`; drag-stop/reset paths in `LayoutCanvas.tsx` and `api/graphLayout.ts` | Load only the addressed project/view; same-user live update; reset deletes that scope. Never allow one project's positions to contaminate another |

### Device-local persistent exceptions

These are the only current localStorage entries that are not feature state. The cleanup guard may
allow them, but should reject any new browser-persistent key unless this inventory is updated with a
similarly narrow transport justification.

| State item | Owner | Classification and scope | Current storage | Desired server namespace | Mutation sites | Sync expectation and justification |
|---|---|---|---|---|---|---|
| Event replay cursor | Notification WebSocket | **Device-local transport**, browser origin | `localStorage["aq:ws:last_seq"]` | None | `loadLastSeq`, `saveLastSeq`, `clearStoredSeq` in `dashboard/src/ws/useEventStream.ts` | Never roam or broadcast as preference state. It is only an optimization for reconnect; a missing/stale cursor triggers replay or authoritative query refetch |
| Event-stream epoch | Notification WebSocket | **Device-local transport**, browser origin | `localStorage["aq:ws:epoch"]` | None | `loadEpoch`/`saveEpoch` and hello-frame handling in `useEventStream.ts` | Never roam. It invalidates the local replay cursor after daemon/database replacement and has no user-visible meaning |
| Dashboard's own active session identity | Console stream authorization bridge | **Device-local transport**, one authenticated dashboard/session | Read-only `localStorage["aq:session:id"]`; production code has no writer | None; replace the stub with shell/auth connection identity, not a preference API | `useOwnSessionId` reads it in `dashboard/src/panes/console-stream/index.tsx`; tests provide the value | Never follow a user to another device and never synchronize. It is connection identity used to reject a mismatched console stream, not a remembered feature choice |

There are no production `sessionStorage`, IndexedDB, cookie, Cache Storage, service-worker storage,
or URL-hash state boundaries. `navigator.clipboard.writeText` calls are explicit copy actions and do
not persist application state.

## URL and browser-history state

URL state remains client-owned because it is addressable navigation, not a hidden preference. Query
parameters survive reload and can be shared deliberately, but dashboards do not live-synchronize
their URLs. Router `location.state` is still narrower: it belongs only to one tab's history entry.

| State item | Owner | Classification and scope | Current storage | Desired server namespace | Mutation sites | Sync expectation |
|---|---|---|---|---|---|---|
| Selected project, workspace tab, and detail resource ids | Router | **Ephemeral address state**, current history entry/bookmark | URL path (`/projects/:projectId/:tab`, `/tasks/:id`, `/sessions/:id`, `/playbooks/:id`) | None | Route table in `dashboard/src/App.tsx`; `projectNavigation`/`workspaceHref` in `dashboard/src/shell/projectNavigation.ts` | URL is authoritative; normal browser navigation/share semantics only |
| Task query, status, completed visibility, focus root, and activity window | Command-center workspace | **Ephemeral address state**, shareable per URL | Query keys `q`, `status`, `completed`, `focus`, `window` | None | `readTaskFilters`/`writeTaskFilters` in `taskFilters.ts`; `TaskWorkspaceProvider` setters in `TaskWorkspace.tsx` | Preserve across command-center tabs and redirects; no server preference write or cross-dashboard push |
| Agent/pool tiles and pinned pool instance | Agents workspace | **Ephemeral address state**, ordered shareable selection | Repeated `agent` query key, including `pool:<profile>@<instance>` | None | `useAgentSelection.navigateTo`, `select`, `setInstance`, and `close` in `dashboard/src/pages/agents/useAgentSelection.ts` | Reload/bookmark restores the addressed views; target existence is revalidated from server data; no preference sync |
| Agent/pool creation surface | Agents workspace | **Ephemeral address state**, current URL | `add=1|agent|pool` | None | `parseCreateMode` and `useAgentSelection.setAdding` in `useAgentSelection.ts` | Shareable/reloadable while present; form contents remain component-local and are discarded on close |
| One-shot activity-drawer command | Shell | **Ephemeral address command**, current navigation | `openDrawer=events|gates` | None | Legacy redirects in `App.tsx`; read/delete in `AppShellV2.useOpenDrawerParam` | Consume once, update right-surface preference, then remove with `replace`; never retain as server state itself |
| Historical session-attempt address | Session detail | **Ephemeral address state**, current URL | `attempt` and `taskId` query keys | None | read by `dashboard/src/pages/SessionDetail.tsx`; links originate in `TaskSessions.tsx` | Bookmark identifies durable server records; the query values themselves need no synchronization |
| Return routes and remount/reset hints | Router and panes | **Ephemeral history state**, one tab/history entry | `location.state`: `from`, `taskPane`, `restoreTaskPane`, `agentSelection`; plus React Router's `location.key` | None | writers/readers in `TaskSessions.tsx`, detail/pane pages, `App.tsx`, `projectNavigation.ts`, and `useAgentSelection.ts` | Do not persist or synchronize; direct navigation uses safe route fallbacks |

The `since`, `limit`, and `thread_id` query parameters in `api/chat.ts`, terminal `cols`/`rows` in
`ws/terminalSocket.ts`, and stream `after_seq`/`attempt_id` parameters are request/transport
parameters rather than browser navigation state. They are covered by the transport/cache rows below.

## Custom stores, contexts, module caches, and bounded buffers

This table accounts for every app-owned context/external store and every module-level mutable cache
found in production dashboard code. It also includes the bounded page caches most likely to be
mistaken for persistence. Static constant maps/sets and temporary maps created inside a render or
pure function are not state boundaries.

| Store or cache | Owner | Classification and scope | Current storage | Desired server namespace | Mutation sites | Sync expectation |
|---|---|---|---|---|---|---|
| TanStack Query cache | API layer | **Ephemeral**, whole page; server-derived | Module-level `QueryClient` in `dashboard/src/main.tsx` | None | query/mutation hooks under `dashboard/src/api`; WebSocket invalidation in `useEventStream.ts` and `useGraphLive.ts` | Events invalidate/patch it and refetch restores authority; reload starts empty |
| Onboarding preview QueryClient | Project onboarding | **Ephemeral**, preview/wizard | Module-level `previewQueryClient` in `pages/project/onboarding/useProjectRoots.ts` when no app provider is available | None | React Query fetch lifecycle | Server-derived cache only; discard with the wizard/test consumer |
| Shell pane external store | Pane shell | Split: persisted fields are **per-user roaming** above; callbacks, toolbar, shortcuts, and mounted component state are **ephemeral** | `stateRef`, listener set, React context in `panes/store.tsx`; pane host state in `ShellPaneHost.tsx` | Only `user.shell_preferences.right_surface.pane` and `.pane_widths` | provider `open`/`close`/`setArgs`/`setWidth`; pane host toolbar/shortcut setters | Server-sync only the serializable validated fields named above; never serialize functions or component-local state |
| RightSurface context | Shell | Split: kind/tab/width are **per-user roaming** above | React context/state in `shell/useRightSurface.tsx` | `user.shell_preferences.right_surface` | provider setters, `AppShellV2`, `RightSurface.tsx` | Same-user live sync after server migration |
| TaskWorkspace context | Command center | **Ephemeral address projection** | React context derived from route, query params, and project query in `TaskWorkspace.tsx` | None | context setters rewrite URL filters/focus | URL and server queries remain the sources of truth |
| Event-stream singleton | Notifications | **Device-local transport**, page/origin | Module `ws`, reconnect delay/status, timer, and listener sets in `ws/useEventStream.ts` | None | `connect`, socket callbacks, subscription hooks | Exactly one socket per loaded JS module; reconnect and query refetch converge; never roam |
| EventStreamProvider activity buffer | Notifications/activity drawer | **Ephemeral**, page | React context with connection status, up to 500 events, next id, and task-message listeners | None | `addEvent`, `clearEvents`, task-message subscriptions in `ws/EventStreamProvider.tsx` | Lives across route changes under the provider; reload clears it; durable activity must come from server queries |
| Activity-drawer event list | Shell activity drawer | **Ephemeral**, one mounted drawer tab | up to 100 events in `ActivityDrawer.EventsList` React state | None | local `useEventStream` callback in `dashboard/src/shell/ActivityDrawer.tsx` | Recreated when the Events tab mounts and discarded when it unmounts; durable event history comes from the server, not this display buffer |
| Expanded-hierarchy external store | Command center | **Per-user roaming** fields above; listeners are **ephemeral** | Module sets plus `useSyncExternalStore` listener set in `useGraphHierarchy.ts` | Per-project fields in `user.command_center_preferences` | `setExpandedTaskIds`/`toggleExpandedId` | Replace local hydration with server hydration/events; listeners remain in-memory |
| Layout tile store and fetch scheduler | Command-center canvas | **Ephemeral**, component and project/params generation; server-derived | `LayoutStore` maps/sets in `layout-v2/layoutStore.ts`; React state/refs in `useLayoutTiles.ts` | None | `mergeTiles`, `evictFar`, `retainForReflow`, `refetchVisible`; layout API responses | Version changes or project/parameter changes invalidate it; refetch is authoritative |
| React Flow/canvas presentation store | Command-center canvas and playbook graphs | **Ephemeral**, mounted canvas | Third-party `ReactFlowProvider`; `viewport`, `layers`, selection/focus, drag preview, adaptive depth, flow cache and animation refs in `LayoutCanvas.tsx` | None, except density/manual positions/expanded hierarchy already named | React Flow callbacks and effects in `LayoutCanvas.tsx`; analogous playbook/proposal canvases | Pan/zoom, keyboard focus, selection, drag-in-progress, derived nodes/edges, and adaptive rendering reset on remount. Only committed preferences named above synchronize |
| Layout live-refetch registry | Command-center canvas | **Ephemeral**, mounted project layers | Module `Map<projectId, Set<Refetch>>` in `layout-v2/liveRegistry.ts` | None | `registerLayoutRefetch` disposer and `refetchLayout` | Registration mirrors mounts; must empty on unmount; no persistence |
| Jump-to-result target store | Command-center toolbar/canvas | **Ephemeral**, active filter session | Module `target` and listener set plus hook-local hits/index in `layout-v2/useJumpToResult.ts` | None | `publishJumpTarget`, locate effect, `next`, unmount cleanup | Clears when filters/project change or toolbar unmounts; no synchronization |
| Graph live-refresh scheduler | Command center | **Ephemeral**, mounted workspace | Hook-local pending `Map`, timers, dirty flags, and selected-project refs in `useGraphLive.ts` | None | WebSocket handler and coalesced `refresh` | Coalesces local refetches only; reconnect triggers authoritative refresh |
| Terminal input queues | Terminal HTTP input | **Device-local transport**, per session id in one JS module | Module `Map<string, Promise<void>>` plus hook leases in `api/useTerminalInput.ts` | None | `write`, promise `finally`, lease effect, `resume` | Preserve byte ordering only while this page is alive; failures invalidate unsent input; never replay or roam |
| Interactive terminal socket | Terminal WebSocket | **Device-local transport**, one mounted viewer and target session | Closure-owned socket, target `sessionId`, dimensions, readiness, and byte backlogs in `ws/terminalSocket.ts` | None | `connectTerminal`, `sendInput`, `resize`, `close`, socket callbacks | A viewer owns one socket; detach on unmount; do not persist target identity, buffered input, or output |
| Pane/transcript/console stream buffers | Session and console viewers | **Device-local transport**, mounted viewer | Hook state/refs and `EventSource` instances in `usePaneStream.ts`, `useTranscriptStream.ts`, and `panes/console-stream/hooks.ts`; buffers are bounded at one screen, 2,000 frames, and 5,000 lines respectively | None | stream callbacks, clear/reset effects, reconnect cursors | Reconnect from server support where available; unmount/reload discards buffers; never treat a buffer as durable history |
| Metrics live tail | Metrics page | **Ephemeral**, mounted range | bounded `live` samples in `pages/metrics/useMetricsFeed.ts`, merged with Query data | None | `metrics.tick` listener and history-load reset effect | Server history supersedes overlapping live samples; range/reload resets the tail |
| Metrics range and hidden-series choices | Metrics page | **Ephemeral view controls**, mounted page | `range` and `hidden` React state in `pages/metrics/Metrics.tsx` | None | range buttons and series `toggle` | These describe the current analysis view, reset on page remount, and do not synchronize across dashboards |
| Chat optimistic transcript | Chat page/pane | **Ephemeral**, mounted project/thread | hook state for hydrated/live/pending messages, events, thinking state and seen-id ref in `pages/chat/useChatTranscript.ts` | None | send lifecycle, WebSocket listener, Query hydration | Server message rows are durable; optimistic rows disappear once hydrated or on reload; no client-store persistence |
| Shortcut registry | Shell | **Ephemeral**, mounted shell | `ShortcutsProvider` contexts/state and module id counter in `shell/hotkeys/useShortcuts.tsx` | None | `useShortcut` register/dispose effects | Registry reflects mounted actions; clear naturally on unmount/reload |
| Command-palette state and action registry | Shell | **Ephemeral**, mounted shell | React contexts/state in `shell/palette/paletteState.tsx` and `shell/palette/registerActions.tsx` | None | palette toggle/setter and action register/dispose effects | Open/query/action callbacks are current-interaction state; never synchronize |
| Project-onboarding wizard store | Project onboarding | **Ephemeral draft**, one modal instance | reducer/context, request id, field registry, errors, and focus state under `pages/project/onboarding/` | None | wizard reducer/dispatch and submit lifecycle in `ProjectOnboardingWizard.tsx` | Closing/reloading discards the draft. Successfully submitted project data is server-owned; transient form contents must not roam |
| Pane/page display modes and filters | Detail, agent, session, settings, metrics, event, and playbook pages | **Ephemeral view controls**, mounted component/resource | component React state, including detail tabs; agent/pool terminal-vs-settings tab; session transcript-vs-pane and stream toggle; pane narrow/filter/follow-tail choices; playbook event scope/advanced/selection; event category/auto-scroll controls | None | the owning component's local setters under `dashboard/src/pages`, `dashboard/src/panes`, and `dashboard/src/shell` | Reset when the component/resource remounts. They describe the current interaction, not a stable preference; if one is intentionally promoted later, it requires a new inventory decision before persistence |
| Local id fallback counters | Nav folder and shortcut registration | **Ephemeral implementation state**, one loaded JS module | `folderCounter` in `shell/navOrganization.ts` and `nextId` in `shell/hotkeys/useShortcuts.tsx` | None | `newFolderId` fallback and `makeId` | Never synchronize. Folder ids become part of shared organization, but normal generation uses `crypto.randomUUID`; the counter is only an in-page uniqueness fallback |

Other component-local `useState`/`useRef` values are conventional transient UI state: modal open
flags, form drafts, validation/submission state, copy/confirmation affordances, table filters that are
not in the URL, scroll/measurement state, and animation/timer bookkeeping. They are **ephemeral** by
definition and must remain out of dashboard-state namespaces. The tables above call out every case
that crosses components, lives at module scope, survives routes, buffers server data, or currently
touches browser persistence.

## Already server-backed UI layout state

Two nearby layout systems are not candidates for the new preference namespaces:

| State item | Owner | Classification and scope | Current storage | Desired server namespace | Mutation sites | Sync expectation |
|---|---|---|---|---|---|---|
| Playbook semantic-graph node layout | Playbook definition/artifact | **Shared workspace**, per playbook artifact | Already persisted by the playbook graph API | Existing playbook artifact/layout model; not `dashboard_state` | `PlaybookSemanticGraphView.onSaveLayout` calls `useSavePlaybookGraphLayout`; canvas drag completion supplies positions | Keep server-authoritative and shared; normal Query invalidation/refetch applies |
| Canonical command-center graph geometry | Task graph layout engine | **Shared workspace derived state**, per project and variant | Already persisted by the server layout engine and served as extents/tiles | Existing task-layout tables/API; not a user preference namespace | Server task/dependency changes and dashboard tidy/reset mutations in `api/graphLayout.ts` | All viewers consume the same revisioned geometry. Per-user density, expansion, and manual overrides remain the separate rows above |

Ordinary task, project, agent, profile, configuration, playbook, message, and gate mutations are
domain records rather than dashboard UI state. They remain server-authoritative and are outside this
migration inventory.

## Audit evidence and completeness boundary

The production tree was searched for:

- browser persistence APIs: `localStorage`, `sessionStorage`, IndexedDB/`IDB*`, cookies, Cache
  Storage, service workers, `BroadcastChannel`, and `window.name`;
- URL/history state: `useSearchParams`, `URLSearchParams`, `location.search`, `location.hash`, and
  `location.state`;
- app stores and durable-looking memory: context providers, `useSyncExternalStore`, module-level
  mutable `Map`/`Set`/connection variables, Query clients, stream buffers, and layout stores.

Production `localStorage` use is fully represented by these 12 keys or key families:

1. `aq.shell.project-organization`
2. `aq:shellpane:width:<viewId>`
3. `aq:rightsurface:width`
4. `aq:flock:collapsed`
5. `aq.dashboard.lastProjectId`
6. `aq.command-center.graph-density`
7. `aq:command-center:expanded-task-ids:v1`
8. `aq:command-center:expanded-finished-task-ids:v1`
9. `aq.command-center.graph-positions`
10. `aq:ws:last_seq`
11. `aq:ws:epoch`
12. `aq:session:id` (read-only in production)

Tests that seed or clear these keys do not define additional persistence boundaries. Future work
should make rows 1-9 server-backed (with theme and the in-memory right-surface fields added to their
approved namespaces), retain only rows 10-12 as explicitly guarded transport exceptions, and leave
all URL and ephemeral rows out of the durable state API.
