import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import FocusTaskList, { PAGE_SIZE } from "../FocusTaskList";
import type { Task } from "../../../api/hooks";

const rows = vi.hoisted(() => ({ value: [] as Task[] }));
vi.mock("../../command-center/useTaskListRows", () => ({
  useTaskListRows: () => ({ rows: rows.value, isLoading: false, error: false, names: new Map([["p1", "First project"]]), heldById: new Map() }),
}));
vi.mock("../../command-center/TaskWorkspace", () => ({
  useTaskWorkspace: () => ({
    projectId: undefined, projects: [{ id: "p1", name: "First project" }],
    filters: { query: "", status: "", showCompleted: false, focus: "", window: "", held: false },
  }),
}));

function Location() {
  const l = useLocation();
  return <output aria-label="Current location">{l.pathname}{l.search}</output>;
}

const renderAt = (path: string) =>
  render(<MemoryRouter initialEntries={[path]}><FocusTaskList /><Location /></MemoryRouter>);
const cards = () => within(screen.getByRole("list", { name: "Tasks" })).getAllByRole("link");

beforeEach(() => {
  rows.value = Array.from({ length: 120 }, (_, i) => ({ id: `t${i + 1}`, project_id: "p1", title: `Task ${i + 1}`, status: "READY" }) as Task);
});

describe("FocusTaskList", () => {
  it("shows 50 cards per page in the list's order", () => {
    renderAt("/focus");
    expect(PAGE_SIZE).toBe(50);
    expect(cards()).toHaveLength(50);
    expect(cards()[0]).toHaveAttribute("href", "/focus/tasks/t1");
    expect(screen.getByText("120 tasks · page 1 of 3")).toBeInTheDocument();
  });

  it("pages through the URL and stops at the ends", () => {
    renderAt("/focus");
    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
    act(() => screen.getByRole("button", { name: "Next page" }).click());
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/focus?page=2");
    expect(cards()[0]).toHaveAttribute("href", "/focus/tasks/t51");
    act(() => screen.getByRole("button", { name: "Next page" }).click());
    expect(cards()).toHaveLength(20);
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
  });

  it("clamps a stale page number and resets the page when a filter changes", () => {
    renderAt("/focus?page=9");
    expect(screen.getByText("120 tasks · page 3 of 3")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search tasks" }), { target: { value: "Task 1" } });
    expect(screen.getByLabelText("Current location")).not.toHaveTextContent("page=");
    expect(screen.getByLabelText("Current location")).toHaveTextContent("q=Task+1");
  });

  it("the project choice lives in the URL", () => {
    renderAt("/focus");
    fireEvent.change(screen.getByRole("combobox", { name: "Project" }), { target: { value: "p1" } });
    expect(screen.getByLabelText("Current location")).toHaveTextContent("project=p1");
  });
});
