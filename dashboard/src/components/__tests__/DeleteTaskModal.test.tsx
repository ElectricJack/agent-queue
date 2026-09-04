import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import DeleteTaskModal from "../DeleteTaskModal";

const remove = vi.hoisted(() => vi.fn());
vi.mock("../../api/hooks", () => ({
  useDeleteTask: () => ({ mutateAsync: remove, isPending: false }),
}));

describe("DeleteTaskModal", () => {
  it("deletes the task and its descendants after confirmation", async () => {
    remove.mockResolvedValue({ deleted: "parent" });
    const close = vi.fn();
    render(
      <DeleteTaskModal
        open
        onClose={close}
        task={{ id: "parent", title: "Parent task", status: "READY" } as never}
      />,
    );

    expect(screen.getByText(/descendant tasks will also be deleted/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Delete task and descendants" }));
    expect(remove).toHaveBeenCalledWith({ task_id: "parent", cascade: true });
    expect(close).toHaveBeenCalledOnce();
  });
});
