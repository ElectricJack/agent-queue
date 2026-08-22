// Bare fetch helpers for the handful of routes that aren't part of the
// auto-generated client (/health, /ready, /plans/{task_id},
// /tasks/{id}/files, /tasks/{id}/file). All command endpoints go through
// the typed SDK in client.ts — do not reach for these for new code.

const BASE_URL = import.meta.env.VITE_API_URL || "";

export async function apiGet<T = unknown>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`);
  if (!res.ok) {
    throw new Error(`API ${res.status}: ${await res.text()}`);
  }
  return res.json() as Promise<T>;
}

// Raw fetch, non-throwing on non-2xx — for callers that need to branch on
// status codes themselves (e.g. the task file endpoint's 403/404/413
// responses) rather than treating every non-2xx as a generic error.
export async function legacyFetch(path: string): Promise<Response> {
  return fetch(`${BASE_URL}${path}`);
}
