import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import RemoveTaskModal from "../RemoveTaskModal";

const deletion = vi.hoisted(() => vi.fn());
vi.mock("../../api/hooks", () => ({
  useDeleteTask: () => ({ mutateAsync: deletion, isPending: false }),
  useDeleteTaskPreview: () => ({ isFetching: false, data: { task_ids: ["parent"], tasks: [] } }),
}));

it("routes compatibility callers through the same delete confirmation", async () => {
  deletion.mockResolvedValue({ warnings: [] });
  const removed = vi.fn();
  render(<RemoveTaskModal task={{ id: "parent", title: "Parent" } as never}
    onClose={vi.fn()} onRemoved={removed} />);
  await userEvent.click(screen.getByRole("button", { name: "Delete selected tasks" }));
  expect(deletion).toHaveBeenCalledWith({ task_id: "parent", recursive: false,
    branches: "keep", stop_running: true, confirmed: true, reason: "" });
  expect(removed).toHaveBeenCalledOnce();
});
