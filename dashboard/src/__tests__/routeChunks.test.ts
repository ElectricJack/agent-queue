import { describe, expect, it, vi } from "vitest";

const CHUNKS = {
  shell: "../shell/AppShellV2",
  workspace: "../pages/CommandCenter",
  tasks: "../pages/command-center/Tasks",
  graph: "../pages/command-center/Graph",
  agents: "../pages/agents/AgentWorkspace",
  metrics: "../pages/metrics/Metrics",
};

/** The route chunks one preload requests: each mock factory runs on its module's first import. */
async function chunksFor(pathname: string): Promise<string[]> {
  const loaded = new Set<string>();
  vi.resetModules();
  for (const [name, path] of Object.entries(CHUNKS)) {
    vi.doMock(path, () => { loaded.add(name); return { default: () => null }; });
  }
  const { preloadInitialRouteChunks } = await import("../routeChunks");
  preloadInitialRouteChunks(pathname);
  // Every requested import has settled once a macrotask has passed.
  await new Promise((resolve) => setTimeout(resolve, 20));
  return [...loaded].sort();
}

describe("preloadInitialRouteChunks", () => {
  it("requests a cold Tasks page's shell, workspace and tab together", async () => {
    expect(await chunksFor("/projects/perf-fixture/tasks")).toEqual(["shell", "tasks", "workspace"]);
  });

  it("requests the graph tab, not the task list, for a cold graph", async () => {
    expect(await chunksFor("/projects/p/graph")).toEqual(["graph", "shell", "workspace"]);
  });

  it("requests the agent workspace for a cold /agents", async () => {
    expect(await chunksFor("/agents")).toEqual(["agents", "shell"]);
  });

  it("requests the metrics page for a cold /metrics", async () => {
    expect(await chunksFor("/metrics")).toEqual(["metrics", "shell"]);
  });

  it("requests only the shell for other shell routes", async () => {
    expect(await chunksFor("/projects/p/overview")).toEqual(["shell"]);
    expect(await chunksFor("/reviews")).toEqual(["shell"]);
  });

  it("leaves focus routes, which render outside the shell, alone", async () => {
    expect(await chunksFor("/focus/tasks/t-1")).toEqual([]);
  });
});
