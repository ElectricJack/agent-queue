import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RemoveTaskModal from "../RemoveTaskModal";

const remove = vi.hoisted(() => vi.fn());
vi.mock("../../api/hooks", () => ({
  useRemoveTask: () => ({ mutateAsync: remove, isPending: false }),
}));
beforeEach(() => { remove.mockReset(); });

const task = { id: "parent", title: "Dead epic", status: "PAUSED" } as never;

describe("RemoveTaskModal", () => {
  it("confirms once with a reason and preserves branches and audit", async () => {
    const close = vi.fn();
    const removed = vi.fn();
    remove.mockResolvedValue({ removed: "parent", disposition: "archived" });
    render(<RemoveTaskModal task={task} onClose={close} onRemoved={removed} />);
    expect(screen.getByRole("dialog")).toHaveTextContent(/Running sessions will stop/);
    expect(screen.getByRole("dialog")).toHaveTextContent(/Branches will stay/);
    await userEvent.clear(screen.getByLabelText("Reason"));
    expect(screen.getByRole("button", { name: "Remove task and descendants" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Reason"), "Superseded");
    await userEvent.click(screen.getByRole("button", { name: "Remove task and descendants" }));
    expect(remove).toHaveBeenCalledOnce();
    expect(remove).toHaveBeenCalledWith({ task_id: "parent", confirmed: true, reason: "Superseded" });
    expect(close).toHaveBeenCalledOnce();
    expect(removed).toHaveBeenCalledOnce();
  });

  it("keeps the dialog open and explains a live writer refusal", async () => {
    const close = vi.fn();
    remove.mockRejectedValue(Object.assign(new Error("API 422"), { payload: {
      code: "hierarchy.integration_owned", error: "hierarchy.integration_owned: A writer is still active; retry after it stops.",
    } }));
    render(<RemoveTaskModal task={task} onClose={close} />);
    await userEvent.click(screen.getByRole("button", { name: "Remove task and descendants" }));
    expect(screen.getByRole("alert")).toHaveTextContent("A writer is still active; retry after it stops.");
    expect(close).not.toHaveBeenCalled();
  });
});
