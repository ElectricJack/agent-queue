/** Entering a container is navigation; changing a filter is not.
 *
 *  Containers are never expanded in place (operator decision 2026-09-20), so
 *  the container you are inside is addressable state: it lives in the `focus`
 *  URL parameter, entering one PUSHES a history entry so the browser's Back
 *  button goes up a level, and filter edits keep replacing.
 */
import type { ReactNode } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation, useNavigate, useNavigationType } from "react-router-dom";
import { TaskWorkspaceProvider, useTaskWorkspace } from "../TaskWorkspace";

vi.mock("../../../api/hooks", () => ({
  useProjects: () => ({ data: [{ id: "alpha", name: "Alpha" }], isLoading: false, error: null }),
}));
vi.mock("../useGraphLive", () => ({ useGraphLive: () => {} }));
vi.mock("../useGraphHierarchy", async (importOriginal) => ({
  ...await importOriginal<typeof import("../useGraphHierarchy")>(),
  GraphStateProvider: ({ children }: { children: ReactNode }) => children,
}));

function Probe() {
  const { setFocus, setQuery, filters, focusId, goToRunningWork } = useTaskWorkspace();
  const navigate = useNavigate();
  return <>
    <button type="button" onClick={() => setFocus("pkg")}>enter pkg</button>
    <button type="button" onClick={() => setFocus("g0")}>enter g0</button>
    <button type="button" onClick={() => setQuery("needle")}>search</button>
    <button type="button" onClick={() => goToRunningWork({
      task_id: "g0", project_id: "alpha", parent_task_id: "pkg", ancestors: ["pkg"], observed_at: 1, layout_version: 1,
    })}>running work</button>
    <button type="button" onClick={() => navigate(-1)}>back</button>
    <output data-testid="nav">{useNavigationType()}</output>
    <output data-testid="search">{useLocation().search}</output>
    <output data-testid="focus">{focusId ?? ""}</output>
    <output data-testid="completed">{String(filters.showCompleted)}</output>
  </>;
}

function mount(path = "/projects/alpha/graph") {
  return render(<MemoryRouter initialEntries={[path]}><Routes>
    <Route path="projects/:projectId/*" element={<TaskWorkspaceProvider><Probe /></TaskWorkspaceProvider>} />
  </Routes></MemoryRouter>);
}

afterEach(cleanup);

describe("entering a container", () => {
  it("pushes a history entry, so Back goes up a level", () => {
    mount();
    fireEvent.click(screen.getByRole("button", { name: "enter pkg" }));
    expect(screen.getByTestId("nav")).toHaveTextContent("PUSH");
    expect(screen.getByTestId("focus")).toHaveTextContent("pkg");

    fireEvent.click(screen.getByRole("button", { name: "enter g0" }));
    expect(screen.getByTestId("focus")).toHaveTextContent("g0");

    fireEvent.click(screen.getByRole("button", { name: "back" }));
    expect(screen.getByTestId("nav")).toHaveTextContent("POP");
    expect(screen.getByTestId("focus")).toHaveTextContent("pkg");
  });

  it("leaves a filter edit replacing, so Back is never a filter's undo", () => {
    mount();
    fireEvent.click(screen.getByRole("button", { name: "search" }));
    expect(screen.getByTestId("nav")).toHaveTextContent("REPLACE");
    expect(screen.getByTestId("search")).toHaveTextContent("q=needle");
  });

  it("pushes one running-work entry, so Back restores the prior scope and filters", () => {
    mount("/projects/alpha/graph?q=needle&focus=previous");
    fireEvent.click(screen.getByRole("button", { name: "running work" }));
    expect(screen.getByTestId("nav")).toHaveTextContent("PUSH");
    expect(screen.getByTestId("focus")).toHaveTextContent("pkg");
    expect(screen.getByTestId("search")).not.toHaveTextContent("needle");

    fireEvent.click(screen.getByRole("button", { name: "back" }));
    expect(screen.getByTestId("focus")).toHaveTextContent("previous");
    expect(screen.getByTestId("search")).toHaveTextContent("q=needle");
  });

  it("does not turn Show completed on: inside a container the default is the root's", () => {
    mount("/projects/alpha/graph?focus=pkg");
    expect(screen.getByTestId("completed")).toHaveTextContent("false");
  });

  it("still implies completed work for a time range, focused or not", () => {
    mount("/projects/alpha/graph?focus=pkg&window=24h");
    expect(screen.getByTestId("completed")).toHaveTextContent("true");
  });
});
