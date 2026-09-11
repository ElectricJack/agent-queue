import type { EntryView } from "./historyState";

type Pane = EntryView["pane"];

const PROJECT_TABS: Record<string, string> = {
  graph: "Graph",
  tasks: "Tasks",
  overview: "Overview",
  sessions: "Sessions",
  workspaces: "Workspaces",
  playbooks: "Playbooks",
  config: "Config",
};

const SETTINGS_SECTIONS: Record<string, string> = {
  playbooks: "Playbooks",
  profiles: "Profiles",
  "intelligence-classes": "Intelligence classes",
  "project-roots": "Project roots",
  messaging: "Messaging",
  config: "Config",
};

/** The pane arg that names what a pane shows, most specific first. */
const PANE_SUBJECT_ARGS = [
  "title", "runId", "proposalId", "sessionId", "taskId", "playbookId",
  "subjectId", "filePath", "path", "workspaceId", "streamId",
];

function decodeSegment(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

function agentLabel(key: string): string {
  return key.startsWith("pool:") ? "pool " + key.slice("pool:".length).split("@")[0] : key;
}

export interface TitleNames {
  project?: (id: string) => string | undefined;
  pane?: (view: string) => string | undefined;
}

/** A short human title for a route, e.g. "First project · Tasks". */
export function routeTitle(pathname: string, search: string, names: TitleNames = {}): string {
  const [head, id, sub] = pathname.split("/").filter(Boolean).map(decodeSegment);
  switch (head) {
    case "agents": {
      const agents = new URLSearchParams(search).getAll("agent").filter(Boolean);
      return agents.length > 0 ? "Agents: " + agents.map(agentLabel).join(", ") : "Agent flock";
    }
    case "projects":
      if (!id) return "Projects";
      return [names.project?.(id) ?? id, sub ? PROJECT_TABS[sub] ?? sub : null].filter(Boolean).join(" · ");
    case "tasks":
      if (!id) return "Tasks";
      return sub === "files" ? `Task ${id} files` : `Task ${id}`;
    case "sessions":
      return id ? `Session ${id}` : "Sessions";
    case "playbooks":
      return id ? `Playbook ${id}` : "Playbooks";
    case "settings":
      return ["Settings", id ? SETTINGS_SECTIONS[id] ?? id : null].filter(Boolean).join(" · ");
    case "metrics":
      return "Metrics";
    case "command-center":
      return "Command Center";
    default:
      return pathname;
  }
}

export function paneTitle(pane: NonNullable<Pane>, names: TitleNames = {}): string {
  const name = names.pane?.(pane.view) ?? pane.view;
  const args = (typeof pane.args === "object" && pane.args !== null ? pane.args : {}) as Record<string, unknown>;
  const subjectKey = PANE_SUBJECT_ARGS.find((key) => typeof args[key] === "string" && args[key] !== "");
  return subjectKey ? `${name} ${String(args[subjectKey])}` : name;
}

/** The title Back / Forward show for an entry: its route, then its pane. */
export function viewTitle(
  view: { pathname: string; search: string; pane: Pane },
  names: TitleNames = {},
): string {
  const route = routeTitle(view.pathname, view.search, names);
  return view.pane ? `${route} — ${paneTitle(view.pane, names)}` : route;
}
