import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { testQueryClient } from "../../../testUtils/dashboardState";
import FocusShell from "../FocusShell";
import FocusTask from "../FocusTask";

const query = vi.hoisted(() => ({
  current: { data: undefined as { id: string; title: string } | undefined, isError: false, error: null as Error | null, refetch: vi.fn() },
}));
vi.mock("../../../api/hooks", () => ({ useTask: () => query.current }));
vi.mock("../../../panes/task-detail/TaskDetailBody", () => ({
  default: (props: { taskId: string; onOpenTask: (id: string) => void; onClose: () => void; host?: unknown }) => (
    <div>
      <output aria-label="Body task">{props.taskId}</output>
      <output aria-label="Pane host">{String(Boolean(props.host))}</output>
      <button onClick={() => props.onOpenTask("fixture-task-1.1")}>Open related</button>
      <button onClick={props.onClose}>Leave detail</button>
    </div>
  ),
}));

function Location() {
  const location = useLocation();
  return <output aria-label="Current location">{location.pathname}</output>;
}

function renderAt(path: string) {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/focus" element={<FocusShell />}>
            <Route index element={<p>Focus home</p>} />
            <Route path="tasks/:taskId" element={<FocusTask />} />
          </Route>
        </Routes>
        <Location />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  query.current = { data: { id: "t1", title: "Ünïcödé task" }, isError: false, error: null, refetch: vi.fn() };
});

describe("FocusTask", () => {
  it("renders the shared body for the route's task, with no pane host", () => {
    renderAt("/focus/tasks/t1");
    expect(screen.getByLabelText("Body task")).toHaveTextContent("t1");
    expect(screen.getByLabelText("Pane host")).toHaveTextContent("false");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Ünïcödé task");
    expect(screen.getByRole("link", { name: "Open in full dashboard" })).toHaveAttribute("href", "/tasks/t1");
  });

  it("opens a related task as a new focus entry, dotted ids included", async () => {
    renderAt("/focus/tasks/t1");
    act(() => screen.getByRole("button", { name: "Open related" }).click());
    await waitFor(() => expect(screen.getByLabelText("Current location")).toHaveTextContent("/focus/tasks/fixture-task-1.1"));
  });

  it("leaving a cold deep link goes to the focus home", async () => {
    renderAt("/focus/tasks/t1");
    act(() => screen.getByRole("button", { name: "Leave detail" }).click());
    await waitFor(() => expect(screen.getByLabelText("Current location")).toHaveTextContent(/^\/focus$/));
  });

  it("a failed read is a scoped error with Retry and Go back", () => {
    query.current = { data: undefined, isError: true, error: new Error("task not found: t1"), refetch: vi.fn() };
    renderAt("/focus/tasks/t1");
    expect(screen.getByRole("alert")).toHaveTextContent("task not found: t1");
    act(() => screen.getByRole("button", { name: "Retry" }).click());
    expect(query.current.refetch).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Go back" })).toBeInTheDocument();
  });
});
