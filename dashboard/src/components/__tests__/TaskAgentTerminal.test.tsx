import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import TaskActions from "../TaskActions";
import type { Task } from "../../api/hooks";
import type { FlockAgent } from "../../api/agents";

const api = vi.hoisted(() => ({ listAgents: vi.fn() }));
vi.mock("../../api/client", () => api);
vi.mock("../../api/hooks", () => {
  const mutation = () => ({ mutate: vi.fn(), isPending: false });
  return {
    useStopTask: mutation, usePauseTask: mutation, useResumeTask: mutation,
    useRestartTask: mutation, useSkipTask: mutation,
    useReopenWithFeedback: mutation, useDeleteTask: mutation, useProvideInput: mutation,
  };
});

const task = { id: "task-1", project_id: "demo", status: "IN_PROGRESS", assigned_agent: "worker" } as Task;
const worker = {
  id: "worker", name: "Solar Eagle", current_task_id: task.id,
  current_project_id: task.project_id, session_id: "session-1",
  session_state: "running", session_provider: "tmux",
} as FlockAgent;
const clients: QueryClient[] = [];

function Location() {
  const location = useLocation();
  return <output aria-label="Current location">{location.pathname}{location.search}|{location.state?.agentSelection}</output>;
}

function renderActions(rows: FlockAgent[] = [worker], onOpenTerminal = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  client.setQueryData(["agents", "flock"], rows);
  render(<QueryClientProvider client={client}>
    <MemoryRouter initialEntries={["/projects/demo/command-center/graph?task=task-1"]}>
      <TaskActions task={task} onOpenTerminal={onOpenTerminal} />
      <Location />
    </MemoryRouter>
  </QueryClientProvider>);
  return { client, onOpenTerminal };
}

beforeEach(() => { api.listAgents.mockResolvedValue({ data: { agents: [worker] } }); });
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.clear()); });

describe("task terminal shortcut", () => {
  it("opens the assigned worker as the sole selected agent on its Terminal tab and closes the task pane", async () => {
    const { onOpenTerminal } = renderActions();
    const button = screen.getByRole("button", { name: "Open agent terminal" });
    expect(button).toHaveAttribute("title", "Open Solar Eagle’s terminal");
    await userEvent.setup().click(button);
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/agents?agent=worker|replace");
    expect(onOpenTerminal).toHaveBeenCalledOnce();
  });

  it.each([
    ["different task", { current_task_id: "task-2" }],
    ["different project", { current_project_id: "other" }],
    ["stopped session", { session_state: "stopped" }],
    ["sleeping session", { session_state: "sleeping" }],
    ["missing session", { session_id: null }],
    ["non-tmux session", { session_provider: "acp" }],
  ])("does not offer a terminal for a %s", (_label, overrides) => {
    renderActions([{ ...worker, ...overrides } as FlockAgent]);
    expect(screen.queryByRole("button", { name: "Open agent terminal" })).not.toBeInTheDocument();
  });

  it("tracks reassignment from the live flock even when the task detail still names the old agent", async () => {
    const { client } = renderActions();
    act(() => client.setQueryData(["agents", "flock"], [
      { ...worker, current_task_id: "task-2" },
      { ...worker, id: "new/worker", name: "Fable Raven", session_id: "session-2", session_state: "draining" },
    ]));
    await userEvent.setup().click(screen.getByRole("button", { name: "Open agent terminal" }));
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/agents?agent=new%2Fworker|replace");
  });

  it("removes the shortcut when the session ends", async () => {
    const { client } = renderActions();
    expect(screen.getByRole("button", { name: "Open agent terminal" })).toBeInTheDocument();
    act(() => client.setQueryData(["agents", "flock"], [{ ...worker, session_id: null, session_state: "stopped" }]));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Open agent terminal" })).not.toBeInTheDocument());
  });
});
