// Shell-wide fixtures: what every page asks for at startup, plus the fixture
// project's graph. A route key is "METHOD /path"; a handler gets the parsed JSON
// body and the URL and returns the body, or {__status, body} for an error.
// Shapes are checked against the generated client by `npm -w dashboard run
// typecheck` (tsconfig.layout.json). Page-specific endpoints belong in the
// page's own fixture file, never here.

export const NOW = 1_790_000_000; // a fixed clock: ages and staleness are deterministic
export const PROJECT = "fixture";
/** Long, unbroken, mixed-script: wraps or clamps, never widens the page. */
export const LONG_TITLE =
  "Ünïcödé 長いタスク名の折り返し試験 — " + "ｗ".repeat(60) + " 🚀🧪🔁 " + "no-break-".repeat(20);
export const TASK_IDS = Array.from({ length: 120 }, (_, i) => `fixture-task-${i + 1}`);
/** Child ids carry a dot (src/task_names.py). */
export const CHILD_TASK_ID = "fixture-task-1.1";
export const SESSION = "fixture-session";
export const POOL_SESSION = "fixture-pool-1";

const STATUSES = ["IN_PROGRESS", "READY", "DEFINED", "BLOCKED"];

/** @satisfies {import("@aq/ts-client").ListProjectsResponse} */
const projects = {
  projects: [
    { id: PROJECT, name: "Fixture project" },
    { id: "second", name: "Second project" },
  ],
};

/** @satisfies {import("@aq/ts-client").ProjectGraphResponse} */
const graph = {
  tasks: TASK_IDS.map((id, i) => ({
    id,
    title: i === 0 ? LONG_TITLE : `Fixture task ${i + 1}`,
    status: STATUSES[i % STATUSES.length] ?? "READY",
    priority: 100 + (i % 5),
    assigned_agent_id: i % 3 === 0 ? "worker-a" : null,
  })),
  edges: [],
  gates: [],
  agents: [],
};

/** @satisfies {import("@aq/ts-client").ListAgentsResponse} */
const agents = {
  agents: [
    {
      id: "worker-a", name: "worker-a", profile_id: "standard-high-claude",
      settings: { name: "worker-a", profile_id: "standard-high-claude" },
      session_id: SESSION, session_state: "running", session_provider: "tmux",
      current_task_id: TASK_IDS[0], current_task_title: LONG_TITLE, current_project_id: PROJECT,
    },
    {
      id: "supervisor-global", name: "supervisor", profile_id: "supervisor", role: "supervisor",
      settings: { name: "supervisor", profile_id: "supervisor" },
      session_id: "supervisor-global", session_state: "running", session_provider: "tmux",
    },
  ],
  count: 2,
};

/** @satisfies {import("@aq/ts-client").PoolStatusResponse} */
const pools = {
  success: true,
  pools: [{ profile_id: "deep-high-claude", min_active: 1, desired: 1, running_idle: 0, running_busy: 1, starting: 0, draining: 0, ready: 1 }],
};

/** @satisfies {import("@aq/ts-client").ListSessionsResponse} */
const sessions = {
  success: true,
  sessions: [{ id: POOL_SESSION, name: POOL_SESSION, profile_id: "deep-high-claude", lifecycle: "pool", provider: "tmux", state: "running", started_at: NOW - 600 }],
  count: 1,
  has_more: false,
};

/** @satisfies {import("@aq/ts-client").ProviderUsageResponse} */
const usage = {
  now: NOW,
  snapshots: [
    { id: 1, provider: "claude", window: "weekly", used_percent: 62, observed_at: NOW - 300, last_seen_at: NOW - 300, source: "probe" },
    { id: 2, provider: "codex", window: "weekly", used_percent: 18, observed_at: NOW - 7_200, last_seen_at: NOW - 7_200, source: "transcript" },
  ],
};

/** @satisfies {import("@aq/ts-client").GetProviderAvailabilityApiProvidersAvailabilityGetResponse} */
const availability = { success: true, now: NOW, providers: [] };

/**
 * The dashboard-state bootstrap. `shell` merges into the shell_preferences
 * value, so a check can plant a roaming desktop pane or drawer:
 * `stub.override("POST /api/dashboard/state-list", () => stateDocuments({ right_surface: {...} }))`.
 * @returns {import("@aq/ts-client").DashboardStateListResponse}
 */
export function stateDocuments(shell = {}) {
  return {
    success: true,
    owner_id: "human:local-operator",
    documents: [
      {
        scope: "user", owner_id: "human:local-operator", subject: null, revision: 1, exists: true,
        updated_at: NOW, namespace: "shell_preferences",
        value: {
          theme: "dark", pane_widths: {},
          right_surface: { width: 480, kind: null, activity_tab: "gates", pane: null },
          projects_section_open: true, agent_flock_collapsed: false, last_project_id: PROJECT,
          ...shell,
        },
      },
    ],
  };
}

/**
 * One dashboard-state read: the `stateDocuments()` copy when it holds that
 * namespace and subject, else the server's absent default (revision 0, no value).
 * @param {import("@aq/ts-client").DashboardStateGetRequest} body
 * @returns {import("@aq/ts-client").DashboardStateGetResponse}
 */
function stateDocument(body) {
  const subject = body.subject ?? null;
  const planted = stateDocuments().documents.find((d) => d.namespace === body.namespace && d.subject === subject);
  return {
    success: true,
    document: planted ?? {
      scope: "user", owner_id: "human:local-operator", subject, revision: 0, exists: false,
      updated_at: null, namespace: body.namespace, value: {},
    },
  };
}

/** @satisfies {import("@aq/ts-client").ListProjectRootsResponse} */
const projectRoots = { success: true, roots: [] };

/**
 * @param {import("@aq/ts-client").GetProjectRequest} body
 * @returns {import("@aq/ts-client").GetProjectResponse | {__status: number, body: import("@aq/ts-client").GetProjectError}}
 */
function project(body) {
  const found = projects.projects.find((p) => p.id === body.project_id);
  if (!found) return { __status: 404, body: { error: `Project '${body.project_id}' not found` } };
  return { id: found.id, name: found.name, status: "ACTIVE", repo_default_branch: "main" };
}

export const routes = {
  "GET /health": () => ({ status: "ok" }),
  "GET /ready": () => ({ ready: true }),
  "POST /api/project/list": () => projects,
  "POST /api/project/get": project,
  "POST /api/project/list-roots": () => projectRoots,
  "POST /api/dashboard/state-list": () => stateDocuments(),
  "POST /api/dashboard/state-get": stateDocument,
  "POST /api/dashboard/state-put": (/** @type {import("@aq/ts-client").DashboardStatePutRequest} */ body) => ({
    success: true,
    document: { ...stateDocuments().documents[0], namespace: body.namespace, value: body.value, revision: (body.base_revision ?? 1) + 1 },
  }),
  "POST /api/agent/list": () => agents,
  "POST /api/agent/list-profiles": () => ({ profiles: [], count: 0 }),
  "POST /api/pool/status": () => pools,
  "POST /api/system/session-list": () => sessions,
  "GET /api/providers/usage": () => usage,
  "GET /api/providers/availability": () => availability,
  "POST /api/review/list": () => ({ success: true, reviews: [] }),
  "POST /api/task/gate-list": () => ({ success: true, gates: [] }),
  [`GET /api/projects/${PROJECT}/graph`]: () => graph,
  "GET /api/projects/second/graph": () => ({ tasks: [], edges: [], gates: [], agents: [] }),
};
