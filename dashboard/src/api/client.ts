// Typed daemon client — generated from /openapi.json by @hey-api/openapi-ts.
// Run `npm run generate:ts-client` from the repo root to refresh after changes
// to FastAPI routes or response models.
//
// All SDK functions and request/response types are re-exported below so call
// sites can `import { listProjectProfiles, ListProjectProfilesResponse } from
// "../api/client"` rather than reaching into the workspace package.

import { client } from "@aq/ts-client";

// In dev: vite proxies /api → http://127.0.0.1:8081 (see vite.config.ts).
// In prod: same-origin assumption — both at "".
// VITE_API_URL escape-hatch lets us point the dashboard at a remote daemon.
client.setConfig({ baseUrl: import.meta.env.VITE_API_URL || "" });

// Translate non-2xx responses into thrown Error instances so React Query's
// onError fires. The default hey-api behaviour is to return { data, error }
// without throwing — convenient for libraries, painful for hooks.
client.interceptors.response.use(async (response) => {
  if (!response.ok) {
    let detail: string;
    let payload: unknown;
    try {
      const body: unknown = await response.clone().json();
      payload = body;
      detail = typeof body === "object" && body !== null && "error" in body && typeof body.error === "string"
        ? body.error
        : JSON.stringify(body);
    } catch {
      detail = await response.clone().text();
    }
    const error = new Error(`API ${response.status}: ${detail}`) as Error & { payload?: unknown };
    error.payload = payload;
    throw error;
  }
  return response;
});

export { client };
export * from "@aq/ts-client";
