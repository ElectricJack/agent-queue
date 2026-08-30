import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ProjectWorkspaces from "../Workspaces";
import type { FlockAgent } from "../../../api/agents";
import type { Workspace } from "../../../api/hooks";

const api = vi.hoisted(() => ({ listAgents: vi.fn(), listWorkspaces: vi.fn() }));
vi.mock("../../../api/client", () => api);

let worker: FlockAgent;
let workspace: Workspace;
const clients: QueryClient[] = [];

beforeEach(() => {
  worker = {
    id: "worker-1", name: "Shared worker", profile_id: "implementer", role: "worker", enabled: true,
    state: "busy", workspace_id: null, project_id: "p1", current_project_id: "p1",
    current_task_id: "task-1", current_task_title: "Implement global workers",
    settings: { name: "Shared worker", profile_id: "implementer", enabled: true },
  };
  workspace = {
    id: "ws-1", project_id: "p1", name: "Checkout", workspace_path: "/tmp/checkout",
    source_type: "link", enabled: true, locked_by_agent_id: "worker-1", locked_by_task_id: "task-1",
  };
  api.listAgents.mockImplementation(async () => ({ data: { agents: [worker], count: 1 } }));
  api.listWorkspaces.mockImplementation(async () => ({ data: { workspaces: [workspace] } }));
});

afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});

function renderWorkspaces() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/projects/p1/workspaces"]}>
    <Routes><Route path="/projects/:projectId/workspaces" element={<ProjectWorkspaces />} /></Routes>
  </MemoryRouter></QueryClientProvider>);
}

async function row() {
  await screen.findByText("Checkout");
  return screen.getByRole("row", { name: /Checkout/ });
}

describe("Workspace occupancy with global agents", () => {
  it("uses the workspace lock when the assigned global agent has no workspace_id", async () => {
    renderWorkspaces();
    expect(within(await row()).getByText("busy")).toBeInTheDocument();
    expect(await screen.findByText("Implement global workers")).toBeInTheDocument();
  });

  it("shows a released workspace as idle despite a busy worker's stale workspace pointer", async () => {
    workspace.locked_by_agent_id = null;
    workspace.locked_by_task_id = null;
    worker.workspace_id = "ws-1";
    renderWorkspaces();
    expect(within(await row()).getByText("idle")).toBeInTheDocument();
    expect(within(await row()).queryByText("busy")).not.toBeInTheDocument();
    expect(screen.queryByText("Implement global workers")).not.toBeInTheDocument();
  });

  it("uses the locked task ID when the worker snapshot is assigned to a different task", async () => {
    worker.current_task_id = "task-elsewhere";
    worker.current_project_id = "p2";
    worker.current_task_title = "Unrelated work";
    renderWorkspaces();
    expect(within(await row()).getByText("busy")).toBeInTheDocument();
    expect(within(await row()).getByText("task-1")).toBeInTheDocument();
    expect(screen.queryByText("Unrelated work")).not.toBeInTheDocument();
  });

  it("keeps a task lock busy when the worker definition is unavailable", async () => {
    api.listAgents.mockResolvedValue({ data: { agents: [], count: 0 } });
    renderWorkspaces();
    expect(within(await row()).getByText("busy")).toBeInTheDocument();
    expect(within(await row()).getByText("task-1")).toBeInTheDocument();
  });
});
