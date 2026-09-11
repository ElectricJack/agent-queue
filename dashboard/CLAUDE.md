# Dashboard (`dashboard/`)

Vite + React 19 + TanStack Query + Tailwind v4. Read-only-ish admin UI for the
agent-queue daemon. All daemon I/O goes through a generated TypeScript client.

## API access

```
@aq/ts-client (workspace package, generated)
        │
        ▼
dashboard/src/api/client.ts   ← configures baseUrl + throwing interceptor
        │
        ▼
dashboard/src/api/hooks.ts    ← React Query hooks (one per command)
        │
        ▼
components / pages
```

- **Never call `fetch` directly** for daemon endpoints — import the SDK function
  from `../api/client` (or use one of the existing hooks).
- The SDK is generated from the daemon's live `/openapi.json`. To refresh after
  changing FastAPI routes or response models, run **from the repo root**:
  ```
  npm run generate:ts-client     # daemon must be running
  npm run generate:ts-client -- --from-file   # use the cached spec at openapi.json
  ```
- New backend command? Add a Pydantic response model in `src/api/models/<category>.py`
  and register it in that module's `RESPONSE_MODELS` dict. Without it, the
  generated TS type will be `unknown`.
- The `legacy-fetch.ts` helper exists only for routes that aren't in the
  generated SDK (`/health`, `/ready`, `/plans/{task_id}`). Don't reach for it
  from new code.

## Conventions

- React Query keys: `[entity, ...filters]`. Mutations invalidate the relevant
  list + detail queries on success — see `invalidateMcpViews` /
  `invalidateProfileViews` for the pattern.
- Errors: the client interceptor throws on non-2xx, so React Query's `error` /
  `isError` work normally. Don't check `result.error` after `mutateAsync` — it
  doesn't exist on the success branch.
- Icons: `@heroicons/react/24/outline` (or `/solid` where the design calls for
  it). Don't introduce other icon libraries.
- Project field names match the daemon: `repo_url`, `repo_default_branch`,
  `assigned_agent`. The hand-typed interfaces that previously lied about
  `repo_path` / `default_branch` / `agent_name` are gone.

## Browser storage

Persistent feature state lives on the server. Anything a user expects to find
again after a reload — on this machine or another — goes through
`useDashboardDocument` and the `dashboard_state` namespaces
(`docs/superpowers/specs/2026-09-10-dashboard-state-contract-design.md`);
addressable navigation goes in the URL; everything else is component or module
memory that a reload discards. There is no browser fallback and no migration of
old values: while the server is unavailable the dashboard renders defaults.

`src/deviceLocal.ts` is the only module that touches browser persistence, and
only for these device-local transport keys:

| Key | Owner | Why it stays in the browser |
|---|---|---|
| `aq:ws:last_seq` | `ws/useEventStream.ts` | This browser's WebSocket replay cursor. A missing or stale value costs a replay or a refetch; it means nothing on another device. |
| `aq:ws:epoch` | `ws/useEventStream.ts` | Event-stream epoch; a change after the daemon's database is replaced invalidates the local cursor. |
| `aq:session:id` | `panes/console-stream/index.tsx` | Read-only connection-identity stub (production never writes it) that rejects a console stream addressed to another session. |

`tests/test_dashboard_browser_storage.py` enforces this in CI (the vitest suite
does not run there). It fails when any other production file under `src/`
references `localStorage`, `sessionStorage`, IndexedDB, cookies, Cache Storage
or `navigator.storage`; when `DEVICE_LOCAL_KEYS` holds a key missing from this
table or the test's allowlist; and when a retired feature key reappears anywhere
under `src/`, tests included. A new key needs a transport justification of the
same kind in all three places — a remembered UI choice never qualifies. Tests
need not assert that a feature leaves browser storage alone; the guard covers it.

History-entry state is navigation, not storage. `shell/navigationHistory.tsx`
records the shell pane each entry showed in that entry's `history.state` (via
`shell/historyState.ts`, and only when `main.tsx` hands it the window's
`History`), so Back / Forward — the top-bar buttons, Alt+←/→, ⌘[ / ⌘], or the
browser's own — put the pane back, even after a reload. It belongs to the one
entry and dies with the tab; it is not a place for remembered UI choices.

## Dev / build

```
npm run dev        # vite dev server, proxies /api → 127.0.0.1:8081
npm run build      # tsc -b && vite build
npm run typecheck  # tsc -b --noEmit
npm run lint       # eslint
```

## Tests

```
npm install                                 # from the repo root
npm run generate:ts-client -- --from-file   # from the repo root
npx vitest run                              # from dashboard/
```

`packages/aq-ts-client/src` is generated and not committed. In a fresh
worktree it does not exist, and without it most test files fail to import
`@aq/ts-client` — generate it once before the first run.

`npm install` is also not optional in a worktree whose `node_modules` predates
a newly added dependency. Node then resolves that package from an *ancestor*
checkout's `node_modules`, and a package resolved from there resolves its own
`react` from there too — two React copies in one render, which surfaces as
`TypeError: Cannot read properties of null (reading 'useReducer')` deep inside
the dependency (it hit `@tanstack/react-virtual` in `command-center/Tasks.tsx`).
A stack frame pointing at `../../../../node_modules/` is the tell. `resolve.dedupe`
does not fix it: Vitest externalizes `node_modules` deps and loads them through
native `require`, which never reaches Vite's resolver. Run `npm install` from the
repo root instead.

Two invariants keep the suite deterministic; don't undo them without reading
`docs/superpowers/specs/2026-09-01-dashboard-vitest-flakiness.md`:

- **`isolate` stays on.** Sharing a module registry and a jsdom across files
  made the suite order-dependent — leaked `vi.mock` registrations and a
  poisoned `navigator.clipboard` that stopped whole files from being
  collected, with a different victim each run.
- **`maxWorkers` is capped**, from the daemon's `AQ_CPU_SHARE` /
  `AQ_TEST_WORKERS` when running inside a session. Vitest's default is half
  the box's cores, which oversubscribes a machine shared by several agents
  and pushes `findBy*` past its timeout. Override with `VITEST_MAX_WORKERS`.

Stub globals with `Object.defineProperty(..., { configurable: true, writable:
true })`. Omitting `writable` leaves a read-only property that makes a later
`Object.assign` on the same global throw.

`vitest.config.ts` is in `tsconfig.node.json`'s `include`, so a config key that
a Vitest major has removed fails `npm run typecheck` instead of being ignored
at runtime.
