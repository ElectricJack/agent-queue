import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import TaskDetail from "../../pages/TaskDetail";
import TaskDetailPane from "../../panes/task-detail";

const data = vi.hoisted(() => ({ task: {
  id: "t", project_id: "p", title: "Task", description: "Original", status: "READY",
  priority: 100, task_type: "feature", integration_mode: null, intelligence_class: "standard-medium",
  profile_id: null, max_retries: 3, retry_count: 0, skip_verification: false, assigned_agent: null,
} as Record<string, unknown> }));
const api = vi.hoisted(() => ({ editTask: vi.fn(), taskSet: vi.fn(), taskComments: vi.fn(), taskComment: vi.fn() }));
vi.mock("../../api/client", async (load) => ({
  ...await load<typeof import("../../api/client")>(), ...api,
}));
vi.mock("../../api/hooks", async (load) => ({
  ...await load<typeof import("../../api/hooks")>(),
  useTask: () => ({ data: data.task }),
  useProfiles: () => ({ data: [{ id: "worker-deep-high-claude", name: "Deep worker" }] }),
  useIntelligenceClasses: () => ({ data: { success: true, classes: [
    { id: "fast-low" }, { id: "standard-medium" }, { id: "deep-high" }, { id: "custom-x" },
  ] } }),
  useGates: () => ({ data: [] }),
  useTaskAttachments: () => ({ data: { success: true, attachments: [] } }),
  useUploadTaskAttachment: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteTaskAttachment: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("../../panes/store", () => ({ useShellPaneStore: () => ({ open: vi.fn(), close: vi.fn() }) }));
vi.mock("../../pages/task/TaskGraph", () => ({ default: () => null, TaskExplain: () => null }));
vi.mock("../TaskActions", () => ({ default: () => null }));
vi.mock("../TaskSessions", () => ({ default: () => null }));

let client: QueryClient;
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  for (const fn of Object.values(api)) fn.mockReset();
  api.editTask.mockResolvedValue({ data: { updated: "t", fields: ["priority"] } });
  api.taskComments.mockResolvedValue({ data: { comments: [], total: 0, limit: 50, offset: 0 } });
  data.task = { ...data.task, status: "READY", assigned_agent: null };
});
afterEach(() => { cleanup(); client.clear(); });
const noop = () => {};
function mount(surface: "full" | "drawer") {
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/tasks/t"]}>
    {surface === "drawer"
      ? <TaskDetailPane args={{ taskId: "t" }} close={noop} setArgs={noop} setToolbar={noop} setShortcuts={noop} />
      : <Routes><Route path="/tasks/:taskId" element={<TaskDetail />} /></Routes>}
  </MemoryRouter></QueryClientProvider>);
}
const select = (name: string) => screen.getByRole("combobox", { name });
const input = (name: string) => screen.getByRole("spinbutton", { name });

describe("task fields editor", () => {
  it.each(["full", "drawer"] as const)("edits priority, class, type, profile and integration mode in %s", async (surface) => {
    mount(surface);
    expect(screen.getAllByText("standard-medium").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(select("Intelligence class")).toHaveValue("standard-medium");
    expect(Array.from(select("Intelligence class").options).map((o) => o.value))
      .toEqual(["", "fast-low", "standard-medium", "deep-high", "custom-x"]);
    fireEvent.change(input("Priority"), { target: { value: "5" } });
    fireEvent.change(select("Intelligence class"), { target: { value: "deep-high" } });
    fireEvent.change(select("Task type"), { target: { value: "bugfix" } });
    fireEvent.change(select("Profile"), { target: { value: "worker-deep-high-claude" } });
    fireEvent.change(select("Integration mode"), { target: { value: "direct" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.editTask).toHaveBeenCalledWith({
      body: {
        task_id: "t", priority: 5, intelligence_class: "deep-high", task_type: "bugfix",
        profile_id: "worker-deep-high-claude", integration_mode: "direct",
      },
      throwOnError: true,
    }));
    expect(api.taskSet).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument());
  });

  it("sends null to clear class, type and integration mode, and omits untouched fields", async () => {
    data.task = { ...data.task, integration_mode: "pull_request" };
    mount("drawer");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(select("Intelligence class"), { target: { value: "" } });
    fireEvent.change(select("Task type"), { target: { value: "" } });
    fireEvent.change(select("Integration mode"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.editTask).toHaveBeenCalledWith({
      body: { task_id: "t", intelligence_class: null, task_type: null, integration_mode: null },
      throwOnError: true,
    }));
  });

  it("locks routing fields while the task is running and surfaces daemon refusals", async () => {
    data.task = { ...data.task, status: "IN_PROGRESS", assigned_agent: "agent-1" };
    mount("drawer");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(select("Intelligence class")).toBeDisabled();
    expect(select("Profile")).toBeDisabled();
    expect(select("Task type")).toBeEnabled();
    api.editTask.mockRejectedValueOnce(new Error("Task is running or claimed; stop the task before changing its routing."));
    fireEvent.change(input("Priority"), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("stop the task before changing its routing");
    expect(input("Priority")).toHaveValue(1);
  });

  it("keeps an unknown current class selectable and cancels without saving", () => {
    data.task = { ...data.task, intelligence_class: "retired-class" };
    mount("full");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(select("Intelligence class")).toHaveValue("retired-class");
    fireEvent.change(input("Priority"), { target: { value: "9" } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("spinbutton", { name: "Priority" })).toBeNull();
    expect(api.editTask).not.toHaveBeenCalled();
  });
});
