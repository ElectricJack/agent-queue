import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ShortcutsProvider } from "../../../shell/hotkeys/useShortcuts";
import { TaskWorkspaceProvider, useTaskWorkspace } from "../TaskWorkspace";
import TaskToolbar from "../TaskToolbar";

const mocks = vi.hoisted(() => ({
  create: vi.fn(), open: vi.fn(), live: vi.fn(),
  error: null as Error | null,
  projects: [{ id: "alpha", name: "Alpha" }, { id: "beta", name: "Beta" }],
}));
vi.mock("../../../api/hooks", () => ({
  useProjects: () => ({ data: mocks.projects, isLoading: false, error: null }),
  useCreateTask: () => ({ mutate: mocks.create, isPending: false, error: mocks.error }),
}));
vi.mock("../../../panes/store", () => ({ useShellPaneStore: () => ({ open: mocks.open }) }));
vi.mock("../useGraphLive", () => ({ useGraphLive: mocks.live }));

function Probe() {
  const workspace = useTaskWorkspace();
  const location = useLocation();
  return <><TaskToolbar /><output data-testid="scope">{workspace.projectIds.join(",")}</output><output data-testid="query">{location.search}</output></>;
}
function mount(path = "/projects/alpha/graph") {
  return render(<MemoryRouter initialEntries={[path]}><ShortcutsProvider><Routes>
    <Route path="projects/:projectId/*" element={<TaskWorkspaceProvider><Probe /></TaskWorkspaceProvider>} />
    <Route path="command-center/*" element={<TaskWorkspaceProvider><Probe /></TaskWorkspaceProvider>} />
  </Routes></ShortcutsProvider></MemoryRouter>);
}
afterEach(cleanup);
beforeEach(() => { vi.clearAllMocks(); mocks.error = null; });

describe("shared Command Center task controls", () => {
  it("uses sidebar route scope and stores search/status in the URL", async () => {
    mount();
    expect(screen.getByTestId("scope")).toHaveTextContent("alpha");
    await userEvent.type(screen.getByRole("searchbox", { name: "Search tasks" }), "checkout");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Task status" }), "READY");
    expect(screen.getByTestId("query")).toHaveTextContent("q=checkout");
    expect(screen.getByTestId("query")).toHaveTextContent("status=READY");
    expect(mocks.live).toHaveBeenLastCalledWith(["alpha"]);
  });

  it("uses all projects in the global route without a second project selector", () => {
    mount("/command-center/tasks");
    expect(screen.getByTestId("scope")).toHaveTextContent("alpha,beta");
    expect(screen.queryByRole("combobox", { name: "Project" })).not.toBeInTheDocument();
  });

  it("opens Add task with the selected project and supports keyboard form submission", async () => {
    mount("/projects/beta/tasks");
    await userEvent.click(screen.getByRole("button", { name: /Add task/ }));
    expect(screen.getByRole("combobox", { name: "Project" })).toHaveValue("beta");
    await userEvent.type(screen.getByRole("textbox", { name: /Title/ }), "  My new task  ");
    fireEvent.submit(screen.getByRole("form", { name: "Create task" }));
    expect(mocks.create).toHaveBeenCalledWith(expect.objectContaining({ title: "My new task", project_id: "beta" }), expect.any(Object));
  });

  it("does not trigger N while typing, but opens creation from the workspace", async () => {
    mount();
    await userEvent.type(screen.getByRole("searchbox", { name: "Search tasks" }), "new");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    (document.activeElement as HTMLElement).blur();
    fireEvent.keyDown(document.body, { key: "n", code: "KeyN", keyCode: 78, charCode: 78 });
    fireEvent.keyUp(document.body, { key: "n", code: "KeyN", keyCode: 78, charCode: 78 });
    await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());
  });

  it("reveals and selects a newly created task after clearing stale filters", async () => {
    mocks.create.mockImplementationOnce((_body, options) => options.onSuccess({ created: "new-task" }));
    mount("/projects/alpha/graph?q=old&status=FAILED");
    await userEvent.click(screen.getByRole("button", { name: /Add task/ }));
    await userEvent.type(screen.getByRole("textbox", { name: /Title/ }), "New work");
    fireEvent.submit(screen.getByRole("form", { name: "Create task" }));
    expect(mocks.open).toHaveBeenCalledWith("task-detail", { taskId: "new-task" });
    expect(screen.getByRole("searchbox", { name: "Search tasks" })).toHaveValue("");
    expect(screen.getByRole("combobox", { name: "Task status" })).toHaveValue("");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("reports creation errors and leaves the draft available", async () => {
    mocks.error = new Error("Project is unavailable");
    mount();
    await userEvent.click(screen.getByRole("button", { name: /Add task/ }));
    await userEvent.type(screen.getByRole("textbox", { name: /Title/ }), "Keep this draft");
    expect(screen.getByRole("alert")).toHaveTextContent("Project is unavailable");
    expect(screen.getByRole("textbox", { name: /Title/ })).toHaveValue("Keep this draft");
  });
});
