import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { useState } from "react";
import TaskActions from "../TaskActions";
import TaskDetail from "../../pages/TaskDetail";
import TaskDetailPane from "../../panes/task-detail";
import { InlineStatus, RowActions } from "../../pages/command-center/TaskRowActions";

const data = vi.hoisted(() => ({ task: {
  id: "t", project_id: "p", title: "Task", description: "Original", status: "READY",
} }));
const api = vi.hoisted(() => ({
  pauseTask: vi.fn(), resumeTask: vi.fn(), taskSet: vi.fn(), editTask: vi.fn(),
  taskComments: vi.fn(), taskComment: vi.fn(),
}));
vi.mock("../../api/client", async (load) => ({
  ...await load<typeof import("../../api/client")>(), ...api,
}));
vi.mock("../../api/hooks", async (load) => ({
  ...await load<typeof import("../../api/hooks")>(),
  useTask: () => ({ data: data.task }),
  useProjectProfiles: () => ({ data: { agent_types: [] } }),
  useGates: () => ({ data: [] }),
}));
vi.mock("../../panes/store", () => ({ useShellPaneStore: () => ({ open: vi.fn(), close: vi.fn() }) }));
vi.mock("../../pages/task/TaskGraph", () => ({ default: () => null, TaskExplain: () => null }));

let client: QueryClient;
beforeEach(() => {
  data.task = { id: "t", project_id: "p", title: "Task", description: "Original", status: "READY" };
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  for (const fn of Object.values(api)) fn.mockReset();
  api.pauseTask.mockResolvedValue({ data: { task_id: "t", status: "PAUSED" } });
  api.resumeTask.mockResolvedValue({ data: { task_id: "t", status: "READY" } });
  api.taskSet.mockResolvedValue({ data: { id: "t", fields_changed: ["description"] } });
  api.editTask.mockResolvedValue({ data: { updated: "t", fields: ["title"] } });
  api.taskComments.mockResolvedValue({ data: { comments: [], total: 0, limit: 50, offset: 0 } });
});
afterEach(() => { cleanup(); client.clear(); });
const noop = () => {};
function mount(surface: "actions" | "row" | "full" | "drawer" | "status") {
  function Harness() {
    const [, redraw] = useState(0);
    const navigate = useNavigate();
    return <>
      <button onClick={() => navigate("/tasks/peer")}>Open peer</button>
      <button onClick={() => redraw((n) => n + 1)}>Refresh task</button>
      {surface === "actions" ? <TaskActions task={data.task} /> :
        surface === "row" ? <RowActions task={data.task} /> :
        surface === "status" ? <InlineStatus task={data.task} /> :
        surface === "drawer" ? <TaskDetailPane args={{ taskId: "t" }} close={noop} setArgs={noop} setToolbar={noop} setShortcuts={noop} /> :
        <Routes><Route path="/tasks/:taskId" element={<TaskDetail />} /></Routes>}
    </>;
  }
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/tasks/t"]}><Harness /></MemoryRouter></QueryClientProvider>);
}
function refresh(task: Partial<typeof data.task>) {
  data.task = { ...data.task, ...task };
  fireEvent.click(screen.getByRole("button", { name: "Refresh task" }));
}

describe("manual task controls", () => {
  it.each(["actions", "row"] as const)("pauses READY and explicitly resumes PAUSED through %s", async (surface) => {
    mount(surface);
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(api.pauseTask).toHaveBeenCalledWith({ body: { task_id: "t" }, throwOnError: true }));
    refresh({ status: "PAUSED" });
    expect(screen.queryByRole("button", { name: "Restart" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(api.resumeTask).toHaveBeenCalledWith({ body: { task_id: "t" }, throwOnError: true }));
  });
  it.each(["actions", "row"] as const)("shows pending and failure feedback in %s", async (surface) => {
    let reject!: (error: Error) => void;
    api.pauseTask.mockReturnValue(new Promise((_, fail) => { reject = fail; }));
    mount(surface);
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Pausing/ })).toBeDisabled());
    await act(async () => reject(new Error("Session could not stop")));
    expect(await screen.findByRole("alert")).toHaveTextContent("Session could not stop");
    expect(screen.getByRole("button", { name: "Pause" })).toBeEnabled();
  });
  it("does not offer the status dropdown as a pause bypass", () => {
    data.task.status = "PAUSED";
    mount("status");
    expect(screen.getByRole("combobox", { name: "Status for Task" })).toBeDisabled();
  });
});

describe("description editing", () => {
  it.each(["full", "drawer"] as const)("saves only description with its original CAS baseline in %s", async (surface) => {
    mount(surface);
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    const textbox = screen.getByRole("textbox", { name: "Description" });
    fireEvent.change(textbox, { target: { value: "My draft" } });
    refresh({ description: "Other person's update", status: "PAUSED" });
    expect(textbox).toHaveValue("My draft");
    api.taskSet.mockRejectedValueOnce(new Error("Description changed; reload before retrying."));
    fireEvent.click(screen.getByRole("button", { name: "Save description" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Description changed");
    expect(textbox).toHaveValue("My draft");
    expect(api.taskSet).toHaveBeenCalledWith({
      body: { task_id: "t", description: "My draft", expected_description: "Original" }, throwOnError: true,
    });
    expect(api.editTask).not.toHaveBeenCalled();
    expect(api.resumeTask).not.toHaveBeenCalled();
  });
  it.each(["full", "drawer"] as const)("can add an empty description and cancel safely in %s", (surface) => {
    data.task.description = "";
    mount(surface);
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Description" }), { target: { value: "Discard" } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel description edit" }));
    expect(screen.queryByRole("textbox", { name: "Description" })).toBeNull();
    expect(api.taskSet).not.toHaveBeenCalled();
  });
});


it("full metadata edit preserves untouched status after a task refresh", async () => {
  mount("full");
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  fireEvent.change(screen.getByDisplayValue("Task"), { target: { value: "Renamed" } });
  refresh({ status: "IN_PROGRESS", description: "Server findings" });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(api.editTask).toHaveBeenCalledWith({
    body: { task_id: "t", title: "Renamed" }, throwOnError: true,
  }));
  expect(api.taskSet).not.toHaveBeenCalled();
});


it("does not carry a metadata draft into another task on navigation", () => {
  mount("full");
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  fireEvent.change(screen.getByDisplayValue("Task"), { target: { value: "Draft for original" } });
  data.task = { ...data.task, id: "peer", title: "Peer" };
  fireEvent.click(screen.getByRole("button", { name: "Open peer" }));
  expect(screen.queryByDisplayValue("Draft for original")).toBeNull();
  expect(screen.getByRole("button", { name: "Edit" })).toBeEnabled();
  expect(api.editTask).not.toHaveBeenCalled();
});
