import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DeleteTaskModal from "../DeleteTaskModal";

const mocks = vi.hoisted(() => ({ deletion: vi.fn(), preview: vi.fn(), fetching: false }));
vi.mock("../../api/hooks", () => ({
  useDeleteTask: () => ({ mutateAsync: mocks.deletion, isPending: false }),
  useDeleteTaskPreview: (input: { recursive: boolean; branches: string }) => {
    mocks.preview(input);
    return { isFetching: mocks.fetching, data: {
      task_ids: input.recursive ? ["parent", "child"] : ["parent"],
      tasks: [{ id: "parent", title: "Parent", status: "IN_PROGRESS" }],
      reparented_tasks: input.recursive ? [] : ["child"],
      branch_details: [{ repository_id: "repo", branch: "aq/parent", action: input.branches,
        remote_sha: "abc123", local_refs: [{ path: "/checkout", sha: "abc123" }],
        worktrees: [{ path: "/worker-slot" }] }],
      sessions: [{ session_id: "running-session", task_id: "parent" }],
      warnings: [{ code: "integration_owned", message: "Live integration authority will be cancelled." }],
    } };
  },
}));
beforeEach(() => {
  mocks.deletion.mockReset(); mocks.preview.mockClear(); mocks.fetching = false;
  mocks.deletion.mockResolvedValue({ warnings: [] });
});
const task = { id: "parent", title: "Parent", status: "IN_PROGRESS" } as never;

describe("DeleteTaskModal", () => {
  it("previews resources and confirms recursive branch deletion once with optional reason", async () => {
    const close = vi.fn(); const deleted = vi.fn();
    render(<DeleteTaskModal task={task} onClose={close} onDeleted={deleted} />);
    expect(screen.getByText("Children kept: child")).toBeInTheDocument();
    expect(screen.getByText(/Stop session running-session/)).toBeInTheDocument();
    expect(screen.getByText("Remote: abc123")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Deletion scope"), "recursive");
    await userEvent.selectOptions(screen.getByLabelText("Branch cleanup"), "delete");
    expect(screen.queryByText("Children kept: child")).not.toBeInTheDocument();
    expect(screen.getByText("Tasks to delete (2)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete selected tasks" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Delete selected tasks" }));
    expect(mocks.deletion).toHaveBeenCalledOnce();
    expect(mocks.deletion).toHaveBeenCalledWith({ task_id: "parent", recursive: true,
      branches: "delete", stop_running: true, confirmed: true, reason: "" });
    expect(close).toHaveBeenCalledOnce(); expect(deleted).toHaveBeenCalledOnce();
  });

  it("shows nonfatal cleanup failures after successful deletion", async () => {
    mocks.deletion.mockResolvedValue({ warnings: [{ code: "cleanup_failed", message: "Remote transport failed" }] });
    const close = vi.fn();
    render(<DeleteTaskModal task={task} onClose={close} />);
    await userEvent.click(screen.getByRole("button", { name: "Delete selected tasks" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Remote transport failed");
    expect(screen.getByRole("dialog")).toHaveTextContent("Task deleted");
    expect(close).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(close).toHaveBeenCalledOnce();
  });

  it("waits for the current preview before enabling confirmation", () => {
    mocks.fetching = true;
    render(<DeleteTaskModal task={task} onClose={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Delete selected tasks" })).toBeDisabled();
  });
});
