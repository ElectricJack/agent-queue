// Endpoints the shared task detail reads. Task ids come from base.mjs.
import { CHILD_TASK_ID, LONG_TITLE, NOW, PROJECT, SESSION, TASK_IDS } from "./base.mjs";

/**
 * @param {import("@aq/ts-client").GetTaskRequest} body
 * @returns {import("@aq/ts-client").GetTaskResponse | {__status: number, body: import("@aq/ts-client").GetTaskError}}
 */
function task(body) {
  const id = body.task_id;
  if (id === CHILD_TASK_ID) {
    return { id, project_id: PROJECT, title: "Dotted child task", status: "READY", parent_task_id: TASK_IDS[0] ?? null };
  }
  const index = TASK_IDS.indexOf(id);
  if (index < 0) return { __status: 404, body: { error: `Task '${id}' not found` } };
  return {
    id, project_id: PROJECT, status: "IN_PROGRESS", priority: 100,
    title: index === 0 ? LONG_TITLE : `Fixture task ${index + 1}`,
    description: "Fixture description with a long unbroken token " + "z".repeat(160),
    assigned_agent: "worker-a", profile_id: "standard-high-claude", created_at: NOW - 3_600, updated_at: NOW - 60,
    subtasks: index === 0 ? [{ id: CHILD_TASK_ID, title: "Dotted child task", status: "READY" }] : [],
    depends_on: [], blocks: [],
  };
}

/** Ids with per-task URL routes: every fixture task, the dotted child, and one that does not exist. */
const MISSING_TASK_ID = "no-such-task";
const PATH_IDS = [...TASK_IDS, CHILD_TASK_ID, MISSING_TASK_ID];
const missing = { __status: 404, body: { detail: "Task not found" } };

/**
 * One attempt on SESSION for the first task, so its watch link is exercised.
 * @param {string} id
 * @returns {import("@aq/ts-client").TaskSessionsResponse | typeof missing}
 */
function sessions(id) {
  if (id === MISSING_TASK_ID) return missing;
  return {
    task_id: id,
    sessions: id !== TASK_IDS[0] ? [] : [{
      id: "fixture-attempt-1", session_id: SESSION, task_id: id, agent_id: "worker-a", agent_name: "worker-a",
      harness: "claude", provider: "tmux", state: "running", work_dir: "/fixture/worktree",
      started_at: NOW - 1_800, session_started_at: NOW - 1_800,
    }],
  };
}

/**
 * @param {string} id
 * @returns {import("@aq/ts-client").TaskAttachmentsResponse | typeof missing}
 */
function attachments(id) {
  return id === MISSING_TASK_ID ? missing : { success: true, attachments: [] };
}

/**
 * @param {import("@aq/ts-client").TaskCommentsRequest} body
 * @returns {import("@aq/ts-client").TaskCommentsResponse}
 */
function comments(body) {
  return { comments: [], total: 0, limit: body.limit ?? 50, offset: body.offset ?? 0 };
}

/**
 * @param {import("@aq/ts-client").TaskSubtasksRequest} body
 * @returns {import("@aq/ts-client").TaskSubtasksResponse}
 */
function subtasks(body) {
  return { success: true, task_id: body.task_id ?? "", subtasks: [], total: 0, settled: 0 };
}

/** @satisfies {import("@aq/ts-client").ListIntelligenceClassesResponse} */
const intelligenceClasses = { success: true, classes: [] };

export const routes = {
  "POST /api/task/get": task,
  "POST /api/task/comments": comments,
  "POST /api/task/subtasks": subtasks,
  "POST /api/system/list-intelligence-classes": () => intelligenceClasses,
  ...Object.fromEntries(PATH_IDS.flatMap((id) => [
    [`GET /api/tasks/${id}/sessions`, () => sessions(id)],
    [`GET /api/tasks/${id}/attachments`, () => attachments(id)],
  ])),
};
