/**
 * Workspace-scoped file browsing endpoints (pane view: file-browser).
 *
 * Both endpoints live outside the generated @aq/ts-client for the same
 * reason as dashboard/src/api/taskFiles.ts: `/file` returns raw
 * `text/plain` (or a binary-reason JSON body), and `browse`'s `entries`
 * shape isn't modelled in the OpenAPI spec either. Per dashboard/CLAUDE.md,
 * legacy-fetch is the right home for routes not in the generated SDK.
 */
import { legacyFetch } from "./legacy-fetch";

export interface WorkspaceBrowseEntry {
  name: string;
  type: "dir" | "file";
  size?: number;
  is_symlink?: boolean;
}

export interface WorkspaceBrowseResponse {
  success: boolean;
  path: string;
  entries: WorkspaceBrowseEntry[];
  reason?: "no_workspace_path";
}

export async function fetchWorkspaceBrowse(
  workspaceId: string,
  path: string,
): Promise<WorkspaceBrowseResponse> {
  const url =
    `/api/workspaces/${encodeURIComponent(workspaceId)}/browse` +
    `?path=${encodeURIComponent(path)}`;
  const res = await legacyFetch(url);
  if (res.status === 404) {
    const body = await res.json().catch(() => ({}) as { detail?: string });
    throw new Error(body.detail ?? "workspace not found");
  }
  if (!res.ok) throw new Error(`browse ${res.status}`);
  return (await res.json()) as WorkspaceBrowseResponse;
}

export async function fetchWorkspaceFileText(
  workspaceId: string,
  path: string,
): Promise<{ text: string; status: number }> {
  const url =
    `/api/workspaces/${encodeURIComponent(workspaceId)}/file` +
    `?path=${encodeURIComponent(path)}`;
  const res = await legacyFetch(url);
  if (res.status === 413) return { text: "File too large to preview (over 512 KB)", status: 413 };
  if (res.status === 403) return { text: "(forbidden path)", status: 403 };
  if (res.status === 404) return { text: "(file not found)", status: 404 };
  if (!res.ok) throw new Error(`file ${res.status}`);
  // A binary file returns JSON {reason: "binary", size, path} instead of
  // scrambled text/plain. Detect by content-type and surface a clear message.
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    try {
      const body = (await res.json()) as { reason?: string; size?: number };
      if (body.reason === "binary") {
        const kb = body.size ? ` (${Math.round(body.size / 1024)} KB)` : "";
        return { text: `(binary file, size:${kb}) — preview not available`, status: 200 };
      }
    } catch {
      // fall through to text
    }
  }
  return { text: await res.text(), status: 200 };
}

/** React Query key helpers — shared between the query definitions in
 * file-browser/index.tsx and its Refresh toolbar action's invalidation
 * calls. */
export function workspaceBrowseKey(workspaceId: string, path: string) {
  return ["workspace-browse", workspaceId, path] as const;
}
export function workspaceFileTextKey(workspaceId: string, path: string | null) {
  return ["workspace-file-text", workspaceId, path] as const;
}
