# Dashboard (`dashboard/`)

Vite + React 19 + TanStack Query + Tailwind v4 admin UI for the daemon. All daemon I/O
goes through the generated TypeScript client: `@aq/ts-client` (workspace package) →
`src/api/client.ts` (baseUrl + throwing interceptor) → `src/api/hooks.ts` (one React
Query hook per command) → components and pages.

## API access

- **Never call `fetch` directly** for daemon endpoints: import the SDK function from
  `../api/client` or use an existing hook. `legacy-fetch.ts` exists only for the routes
  the SDK lacks (`/health`, `/ready`, `/plans/{task_id}`); don't reach for it in new code.
- The SDK is generated from the daemon's `/openapi.json`. Refresh it **from the repo
  root** after changing FastAPI routes or response models:
  ```
  npm run generate:ts-client                  # daemon must be running
  npm run generate:ts-client -- --from-file   # from the committed openapi.json
  ```
- A new backend command needs a Pydantic response model in
  `src/api/models/<category>.py`, registered in that module's `RESPONSE_MODELS`, or its
  generated TS type is `unknown`.

## Conventions

- React Query keys are `[entity, ...filters]`; mutations invalidate the relevant list +
  detail queries on success (see `invalidateMcpViews` / `invalidateProfileViews`).
- The client interceptor throws on non-2xx, so React Query's `error` / `isError` work
  normally; there is no `result.error` after `mutateAsync`.
- Icons: `@heroicons/react/24/outline` (or `/solid` where the design calls for it) only.
- Project field names match the daemon: `repo_url`, `repo_default_branch`, `assigned_agent`.

## Browser storage

Persistent feature state lives on the server: anything a user expects to find again
after a reload goes through `useDashboardDocument` and the `dashboard_state` namespaces
(`docs/superpowers/specs/2026-09-10-dashboard-state-contract-design.md`); addressable
navigation goes in the URL; everything else is component or module memory. There is no
browser fallback and no migration: while the server is unavailable the dashboard renders
defaults.

`src/deviceLocal.ts` is the only module that may touch browser persistence, and only for
these device-local transport keys:

| Key | Owner | Why it stays in the browser |
|---|---|---|
| `aq:ws:last_seq` | `ws/useEventStream.ts` | This browser's WebSocket replay cursor; a missing or stale value costs a replay or a refetch and means nothing on another device. |
| `aq:ws:epoch` | `ws/useEventStream.ts` | Event-stream epoch; a change after the daemon's database is replaced invalidates the local cursor. |
| `aq:session:id` | `panes/console-stream/index.tsx` | Read-only connection-identity stub (production never writes it) that rejects a console stream addressed to another session. |

`tests/test_dashboard_browser_storage.py` enforces this in CI (the vitest suite does not
run there): any other production file referencing `localStorage`, `sessionStorage`,
IndexedDB, cookies, Cache Storage or `navigator.storage` fails, as does a
`DEVICE_LOCAL_KEYS` entry missing from this table or the test's allowlist, or a retired
feature key reappearing anywhere under `src/`. A new key needs a transport justification
of the same kind in all three places; a remembered UI choice never qualifies.

History-entry state is navigation, not storage: `shell/navigationHistory.tsx` records the
pane each entry showed in that entry's `history.state` so Back / Forward restore it, even
after a reload. It dies with the tab and is not a place for remembered UI choices.

## Dev / build / test

```
npm run dev          # vite dev server, proxies /api → 127.0.0.1:8081
npm run build        # tsc -b && vite build
npm run typecheck    # tsc -b --noEmit
npm run lint         # eslint
npm install && npm run generate:ts-client -- --from-file   # from the repo root, once per worktree
npx vitest run       # from dashboard/
```

- `packages/aq-ts-client/src` is generated and not committed: in a fresh worktree most
  test files fail to import `@aq/ts-client` until you generate it.
- `npm install` is not optional in a worktree whose `node_modules` predates a new
  dependency: Node then resolves the package, and its own `react`, from an *ancestor*
  checkout — two React copies in one render, surfacing as
  `TypeError: Cannot read properties of null (reading 'useReducer')` deep inside the
  dependency. A stack frame under `../../../../node_modules/` is the tell; `resolve.dedupe`
  cannot fix it because Vitest loads externalized deps through native `require`.
- Two invariants keep the suite deterministic
  (`docs/superpowers/specs/2026-09-01-dashboard-vitest-flakiness.md`): **`isolate` stays
  on** (a shared module registry and jsdom made the suite order-dependent), and
  **`maxWorkers` is capped** from the daemon's `AQ_CPU_SHARE` / `AQ_TEST_WORKERS` inside a
  session (override with `VITEST_MAX_WORKERS`; Vitest's default of half the cores
  oversubscribes a shared box and pushes `findBy*` past its timeout).
- Stub globals with `Object.defineProperty(..., { configurable: true, writable: true })`;
  omitting `writable` leaves a read-only property that makes a later `Object.assign` throw.
- `vitest.config.ts` is in `tsconfig.node.json`'s `include`, so a config key a Vitest
  major removed fails `npm run typecheck` instead of being silently ignored.
