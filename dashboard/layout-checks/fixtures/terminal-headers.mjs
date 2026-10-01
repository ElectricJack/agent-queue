import { LONG_TITLE, NOW, POOL_SESSION, SESSION } from "./base.mjs";

export const WORKER_NAME = "Builder — " + LONG_TITLE;
export const TERMINAL_SELECTION = "/agents?agent=worker-a&agent=supervisor-global&agent=pool%3Adeep-high-claude";

/**
 * The same three panes for before/after evidence: long names, live and refused connections.
 * @param {{override: (key: string, handler: () => unknown) => void, allowTerminal: (id: string, screen: string) => void}} stub
 */
export function terminalHeaderFixtures(stub) {
  stub.override("POST /api/agent/list", () => ({ agents: [
    { id: "worker-a", name: WORKER_NAME, profile_id: "standard-high-claude", state: "busy",
      settings: { name: WORKER_NAME, profile_id: "standard-high-claude" },
      provider: "anthropic", model: "claude-opus-4-6", intelligence_class: "standard-high",
      session_id: SESSION, session_state: "running", session_provider: "tmux",
      current_task_id: "fixture-task-1", current_task_title: LONG_TITLE },
    { id: "supervisor-global", name: "Supervisor", profile_id: "supervisor", role: "supervisor", state: "idle",
      settings: { name: "Supervisor", profile_id: "supervisor" },
      provider: "openai", model: "gpt-5.6", intelligence_class: "standard-high",
      project_id: "fixture", session_id: "supervisor-global", session_state: "running", session_provider: "tmux" },
  ], count: 2 }));
  stub.override("POST /api/system/session-list", () => ({ success: true, count: 2, sessions: [
    { id: POOL_SESSION, name: "Pool worker with a long session name", profile_id: "deep-high-claude",
      lifecycle: "pool", provider: "tmux", state: "running", started_at: NOW - 600,
      project_id: "fixture", harness: "claude", model: "claude-opus-4-6", intelligence_class: "deep-high",
      task_id: "fixture-task-3", work_dir: "/workspaces/" + "long-workspace-path/".repeat(10) },
    { id: "fixture-pool-2", name: "Second worker", profile_id: "deep-high-claude",
      lifecycle: "pool", provider: "tmux", state: "running", started_at: NOW - 300 },
  ] }));
  stub.allowTerminal(SESSION, "\x1b[32mAQ worker connected\x1b[0m\r\nclaimed: fixture-task-1\r\n$ aq task heartbeat\r\nWorking on a long task title…\r\n");
  stub.allowTerminal(POOL_SESSION, "\x1b[36mAQ pool worker\x1b[0m\r\nclaimed: fixture-task-3\r\n$ npm run typecheck\r\nAll checks passed.\r\n");
}
