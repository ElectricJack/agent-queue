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
