import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import ProjectHeader from "../ProjectLayout";

vi.mock("../../../api/hooks", () => ({
  useProject: () => ({ data: { id: "p1", name: "Agent Queue", repo_url: "https://github.com/acme/agent-queue" }, isLoading: false }),
  useDeleteProject: () => ({ mutateAsync: vi.fn(), isPending: false }),
  usePauseProject: () => ({ mutate: vi.fn(), isPending: false }),
  useResumeProject: () => ({ mutate: vi.fn(), isPending: false }),
}));

afterEach(cleanup);

it("shows the repository as a GitHub link alongside the project name", () => {
  render(<MemoryRouter initialEntries={["/projects/p1/tasks"]}><Routes>
    <Route path="/projects/:projectId/*" element={<ProjectHeader />} />
  </Routes></MemoryRouter>);

  expect(screen.queryByText("Project")).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Agent Queue" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "https://github.com/acme/agent-queue" }))
    .toHaveAttribute("href", "https://github.com/acme/agent-queue");
});
