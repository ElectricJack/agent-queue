# TypeScript client

`@aq/ts-client` is the typed TypeScript client for the
[HTTP API](README.md), generated from
[`openapi.json`](../../../openapi.json) by
[`@hey-api/openapi-ts`](https://heyapi.dev/). The web dashboard is its only
consumer in this repository, and it is how every dashboard button reaches the
daemon.

It differs from the [Python client](python-client.md) in one important way:
**the generated output is not committed**. `packages/aq-ts-client/src/` is
ignored by Git and rebuilt on demand, so only the committed `openapi.json` has
to stay current.

## Layout

```text
packages/aq-ts-client/
  package.json     the workspace package and its "generate" script  (tracked)
  tsconfig.json                                                     (tracked)
  src/index.ts     re-exports the two generated files               (generated)
  src/sdk.gen.ts   one exported function per operation              (generated)
  src/types.gen.ts request/response/error types                     (generated)
```

> **Note.** `.gitignore` excludes `packages/aq-ts-client/src/` and then tries
> to re-include `src/index.ts`; Git does not descend into an excluded
> directory, so the negation has no effect and the whole tree — `index.ts`
> included — is untracked. Nothing depends on it being tracked: the generate
> step runs before dev, build and typecheck.

## Generate it

From the repository root:

```bash
npm install                                # once, sets up the workspaces
./scripts/regenerate-ts-client.sh --from-file
```

or, equivalently, `npm run generate:ts-client`. The three modes match the
Python script's: `--from-file` uses the committed spec (canonical),
`--offline` rebuilds the spec from this checkout first, and no flag fetches it
from a running daemon.

You rarely need to run it by hand. The dashboard's `predev`, `prebuild` and
`pretypecheck` scripts each run `npm -w @aq/ts-client run generate`
([`dashboard/package.json`](../../../dashboard/package.json)), so a normal
`npm run dev` in `dashboard/` regenerates the client first.

## Using it directly

```ts
import { taskShow } from "@aq/ts-client";

const { data } = await taskShow({
  body: { task_id: "solid-grove.13" },
  throwOnError: true,
});
console.log(data.id, data.status);
```

Every operation is one exported function named after its `operationId` in
camel case — `task_show` → `taskShow`, `list_tasks` → `listTasks` — and the
generated function already knows its path and method:

```ts
export const taskShow = <ThrowOnError extends boolean = false>(options: OptionsLegacyParser<TaskShowData, ThrowOnError>) => {
    return (options?.client ?? client).post<TaskShowResponse2, TaskShowError, ThrowOnError>({
        ...options,
        url: '/api/task/show'
    });
};
```

By default a call resolves to `{ data, error }` rather than throwing;
`throwOnError: true` inverts that. The dashboard sets up throwing globally
instead — see below.

Operations with no category land in the same flat export list, so the
hand-written resource routes are named after their generated operation id:
`getProviderUsageApiProvidersUsageGet`, for instance. That verbosity is a
consequence of FastAPI deriving an id for routes that have no explicit
`operation_id`; the categorized routes set theirs to the command name, which
is why they read well.

## How the dashboard uses it

[`dashboard/src/api/client.ts`](../../../dashboard/src/api/client.ts) is the
single place the generated client is configured, and every call site imports
from there rather than from the package:

* **Base URL.** `client.setConfig({ baseUrl: import.meta.env.VITE_API_URL || "" })`.
  Empty means same-origin, which is what a built dashboard served by the
  daemon wants. In development, Vite proxies `/api`, `/health`, `/ready` and
  `/ws` to the daemon
  ([`dashboard/vite.config.ts`](../../../dashboard/vite.config.ts)), so the
  browser still talks to its own origin. `VITE_API_URL` points a local
  dashboard at a remote daemon.
* **Errors throw.** A response interceptor turns any non-2xx into
  `new Error("API <status>: <detail>")`, with the parsed body kept on
  `error.payload`. React Query's `onError` only fires on a thrown error, so
  without this every failed mutation would look like a success with empty
  data.
* **Re-export.** `export * from "@aq/ts-client"` means a call site writes
  `import { listTasks, ListTasksResponse } from "../api/client"`.

The layer above is React Query.
[`dashboard/src/api/hooks.ts`](../../../dashboard/src/api/hooks.ts) wraps each
operation in a `useQuery` / `useMutation` with a stable query key:

```ts
export function useTasks(projectId?: string, opts?: { showAll?: boolean }) {
  return useQuery({
    queryKey: ["tasks", projectId, opts?.showAll],
    queryFn: async () => {
      const body: Record<string, unknown> = {};
      if (projectId) body.project_id = projectId;
      if (opts?.showAll) body.show_all = true;
      const { data } = await listTasks({ body, throwOnError: true });
      return (data as ListTasksResponse).tasks ?? [];
    },
    refetchInterval: 60_000,
  });
}
```

Three things sit beside the generated client rather than inside it:

| File | Why it exists |
|---|---|
| [`dashboard/src/api/legacy-fetch.ts`](../../../dashboard/src/api/legacy-fetch.ts) | Bare `fetch` for the routes the generated client does not cover well — `/health`, `/ready`, `/plans/{task_id}`, the task file routes — and for callers that must branch on a status code (403 vs 404 vs 413) instead of catching a generic error. New code should not reach for it. |
| [`dashboard/src/ws/useEventStream.ts`](../../../dashboard/src/ws/useEventStream.ts) | A module-scoped singleton WebSocket to `/ws/events` with exponential backoff, the `after_seq` cursor and the epoch guard. OpenAPI cannot describe a socket, so this is hand-written — see [streaming](events.md). |
| [`dashboard/src/ws/terminalSocket.ts`](../../../dashboard/src/ws/terminalSocket.ts) | The `aq-terminal-v1` terminal attach socket. |

Polling and pushing work together: React Query holds the data, and a frame on
the event stream invalidates the affected keys so the next render refetches.
That is why the WebSocket carries identifiers rather than full objects.

## Version compatibility

`@hey-api/openapi-ts` is pinned as a dev dependency of the package
(`^0.61.0`, with `@hey-api/client-fetch` `^0.6.0` at runtime). Because the
output is regenerated rather than committed, a generator upgrade cannot
produce a stale diff the way it can for the Python client — but it can change
the shape of the generated API, so treat a bump as a change to every call
site.

The compatibility question that matters is the same one as for Python: does
`openapi.json` still describe the daemon you are talking to? The check is in
[`tests/test_api_client_contract.py`](../../../tests/test_api_client_contract.py),
and the [Python client](python-client.md#which-daemon-does-this-client-match)
page has the one-liner that compares a running daemon's operation set with the
committed spec.

## State ownership

The generated client is stateless. The dashboard's state lives in React Query's
cache (memory, per tab) plus two `localStorage` keys used by the event stream
(`aq:ws:last_seq` and `aq:ws:epoch`). Nothing the dashboard stores is
authoritative; the daemon's database is.

## Common failures and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `Cannot find module '@aq/ts-client'` | The client has not been generated, or `npm install` has not run at the root. | `npm install` then `npm run generate:ts-client`. |
| A new endpoint is missing from the SDK | The committed `openapi.json` is behind the daemon. | Regenerate the spec (`./scripts/regenerate-api-client.sh --offline`) and then the TS client. |
| TypeScript errors right after pulling | The generated tree is from an older spec. | Re-run the generate step; `pretypecheck` does it for you. |
| Every request 404s in dev | The Vite proxy is not routing, or `VITE_API_URL` points somewhere else. | Check `dashboard/vite.config.ts` and the daemon's port. |
| A mutation silently "succeeds" on an error | Something bypassed `dashboard/src/api/client.ts` and called the package directly. | Import from `../api/client`, which installs the throwing interceptor. |

## Related pages

* [HTTP API overview](README.md) — the surface this client wraps.
* [Streaming](events.md) — the sockets the dashboard speaks by hand.
* [Python client](python-client.md) — the same spec, the other client, and the
  regeneration rules in full.
* [Response models](models.md) — where the generated types come from.

## Source and tests

[`packages/aq-ts-client/`](../../../packages/aq-ts-client/),
[`scripts/regenerate-ts-client.sh`](../../../scripts/regenerate-ts-client.sh),
[`dashboard/src/api/`](../../../dashboard/src/api/).

```bash
aq test tests/test_api_client_contract.py     # the spec both clients are generated from
npm -w dashboard run typecheck                 # regenerates the client, then typechecks it
```
