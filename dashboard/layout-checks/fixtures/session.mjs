// Sessions the focus session route reads (and SessionDetail, for Task 9).
import { NOW, POOL_SESSION, PROJECT, SESSION, TASK_IDS } from "./base.mjs";

export const STARTED = NOW - 1_200;
export const ENDED_SESSION = "fixture-ended";

/** @type {Record<string, import("@aq/ts-client").SessionSummary>} */
export const SESSIONS = {
  [SESSION]: {
    id: SESSION, name: "worker-a", agent_id: "worker-a", task_id: TASK_IDS[0] ?? null, project_id: PROJECT,
    provider: "tmux", harness: "claude", lifecycle: "task", state: "running", started_at: STARTED,
  },
  "supervisor-global": {
    id: "supervisor-global", name: "supervisor", agent_id: "supervisor-global",
    provider: "tmux", lifecycle: "named", state: "running", started_at: NOW - 86_400,
  },
  [POOL_SESSION]: {
    id: POOL_SESSION, name: POOL_SESSION, provider: "tmux", lifecycle: "pool", state: "running", started_at: NOW - 600,
  },
  [ENDED_SESSION]: {
    id: ENDED_SESSION, name: "worker-a (earlier)", agent_id: "worker-a", provider: "tmux",
    state: "stopped", started_at: NOW - 90_000, ended_at: NOW - 80_000, end_reason: "Task closed: pass",
  },
};

/**
 * @param {import("@aq/ts-client").SessionShowRequest} body
 * @returns {import("@aq/ts-client").SessionShowResponse | {__status: number, body: import("@aq/ts-client").SessionShowError}}
 */
export function showSession(body) {
  const session = SESSIONS[String(body.session_id ?? "")];
  return session ? { session } : { __status: 404, body: { error: `Session '${body.session_id}' not found` } };
}

export const routes = {
  "POST /api/system/session-show": showSession,
};
