import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ProjectPlaybooks from "../Playbooks";

const mock = vi.hoisted(() => ({ del: vi.fn() }));

vi.mock("../../../api/hooks", () => ({
  usePlaybooks: () => ({
    data: [
      {
        id: "audit", scope: "project", scope_identifier: "alpha", triggers: ["task.completed"],
        version: 1, node_count: 3, running_count: 0, enabled: false,
      },
      { id: "elsewhere", scope: "project", scope_identifier: "beta", enabled: false },
    ],
    isLoading: false,
  }),
  useSetPlaybookEnabled: () => ({ mutate: vi.fn(), isPending: false, variables: undefined }),
  useDeletePlaybook: () => ({ mutateAsync: mock.del, isPending: false }),
  usePlaybookActivationHealth: () => ({
    data: {
      activations: [{
        playbook_id: "audit", scope: "project", scope_identifier: "alpha",
        enabled: false, active_artifact_sha256: "c".repeat(64),
      }],
    },
    isLoading: false,
  }),
}));

beforeEach(() => {
  mock.del.mockReset();
  mock.del.mockResolvedValue({ success: true, deleted: true });
});

function show() {
  render(
    <MemoryRouter initialEntries={["/projects/alpha/playbooks"]}>
      <Routes>
        <Route path="/projects/:projectId/playbooks" element={<ProjectPlaybooks />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("ProjectPlaybooks", () => {
  it("deletes this project's playbook from its row", async () => {
    show();

    expect(screen.queryByRole("button", { name: "Delete playbook elsewhere" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Delete playbook audit" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete playbook" }));

    expect(mock.del).toHaveBeenCalledWith({
      playbook_id: "audit", scope: "project", scope_identifier: "alpha",
      artifact_sha256: "c".repeat(64),
    });
  });

  it("keeps the row when the daemon refuses the delete", async () => {
    mock.del.mockRejectedValue(new Error("API 422: playbook is referenced by project integration policy"));
    show();

    await userEvent.click(screen.getByRole("button", { name: "Delete playbook audit" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete playbook" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("referenced by project integration policy");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "audit" })).toBeInTheDocument();
  });
});
