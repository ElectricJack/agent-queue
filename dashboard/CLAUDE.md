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
