import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SystemPlaybooks from "./Playbooks";

const mock = vi.hoisted(() => ({
  del: vi.fn(),
  enabled: false,
}));

vi.mock("../../api/hooks", () => ({
  usePlaybooks: () => ({
    data: [{
      id: "audit", scope: "system", scope_identifier: "", triggers: ["timer.24h"],
      version: 2, node_count: 4, running_count: 0, enabled: mock.enabled,
    }],
    isLoading: false,
  }),
  useSetPlaybookEnabled: () => ({ mutate: vi.fn(), isPending: false, variables: undefined }),
  useDeletePlaybook: () => ({ mutateAsync: mock.del, isPending: false }),
  usePlaybookActivationHealth: () => ({
    data: {
      activations: [{
        playbook_id: "audit", scope: "system", scope_identifier: "",
        enabled: mock.enabled, active_artifact_sha256: "b".repeat(64),
      }],
    },
    isLoading: false,
  }),
}));

beforeEach(() => {
  mock.del.mockReset();
  mock.del.mockResolvedValue({ success: true, deleted: true });
  mock.enabled = false;
});

describe("SystemPlaybooks", () => {
  it("deletes a paused playbook from its row", async () => {
    render(<MemoryRouter><SystemPlaybooks /></MemoryRouter>);

    await userEvent.click(screen.getByRole("button", { name: "Delete playbook audit" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("audit");

    await userEvent.click(screen.getByRole("button", { name: "Delete playbook" }));

    expect(mock.del).toHaveBeenCalledWith({
      playbook_id: "audit", scope: "system", scope_identifier: "",
      artifact_sha256: "b".repeat(64),
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("blocks the delete while the row is still enabled", async () => {
    mock.enabled = true;
    render(<MemoryRouter><SystemPlaybooks /></MemoryRouter>);

    await userEvent.click(screen.getByRole("button", { name: "Delete playbook audit" }));

    expect(screen.getByRole("button", { name: "Delete playbook" })).toBeDisabled();
    expect(mock.del).not.toHaveBeenCalled();
  });

  it("closes the dialog on cancel without deleting", async () => {
    render(<MemoryRouter><SystemPlaybooks /></MemoryRouter>);

    await userEvent.click(screen.getByRole("button", { name: "Delete playbook audit" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mock.del).not.toHaveBeenCalled();
  });
});
