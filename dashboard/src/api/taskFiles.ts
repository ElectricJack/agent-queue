/**
 * Task-scoped worktree file endpoints.
 *
 * Both endpoints live outside the generated @aq/ts-client because the
 * ``/file`` endpoint returns raw ``text/plain`` (per dashboard/CLAUDE.md,
 * legacy-fetch is the right home for routes not modelled in the OpenAPI
 * spec).  ``/files`` could be codegen'd later; keeping both here
 * co-locates the pair.
 */
import { legacyFetch } from "./legacy-fetch";

export interface TaskFileEntry {
  path: string;
  additions: number;
  deletions: number;
  status: string; // A | M | D | R | C | ...
}

export interface TaskFilesResponse {
  success: boolean;
  files: TaskFileEntry[];
  base: string | null;
  workspace_path: string | null;
  reason?: "no_workspace" | "not_a_git_checkout" | "diff_failed";
}

export async function fetchTaskFiles(taskId: string): Promise<TaskFilesResponse> {
  const res = await legacyFetch(`/api/tasks/${encodeURIComponent(taskId)}/files`);
  if (!res.ok) throw new Error(`files ${res.status}`);
  return (await res.json()) as TaskFilesResponse;
}

export async function fetchTaskFileText(
  taskId: string,
  path: string,
): Promise<{ text: string; status: number }> {
  const url =
    `/api/tasks/${encodeURIComponent(taskId)}/file` +
    `?path=${encodeURIComponent(path)}`;
  const res = await legacyFetch(url);
  if (res.status === 413) return { text: "(file exceeds 512 KB cap)", status: 413 };
  if (res.status === 403) return { text: "(forbidden path)", status: 403 };
  if (res.status === 404) return { text: "(file not found)", status: 404 };
  if (!res.ok) throw new Error(`file ${res.status}`);
  return { text: await res.text(), status: 200 };
}
