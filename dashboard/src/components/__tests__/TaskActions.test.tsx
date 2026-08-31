import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import TaskActions from "../TaskActions";

const mockNavigate = vi.fn();
const mockDelete = vi.fn();

vi.mock("react-router-dom", () => ({
  useLocation: () => ({ pathname: "/command-center/graph", search: "?q=needle", state: null }),
  useNavigate: () => mockNavigate,
}));

vi.mock("../../api/hooks", () => {
  const mutation = () => ({ mutate: vi.fn(), isPending: false });
  return {
    useStopTask: mutation,
    usePauseTask: mutation,
    useResumeTask: mutation,
    useRestartTask: mutation,
    useSkipTask: mutation,
    useApproveTask: mutation,
    useApprovePlan: mutation,
    useRejectPlan: mutation,
    useDeletePlan: mutation,
    useReopenWithFeedback: mutation,
    useProvideInput: mutation,
    useDeleteTask: () => ({ mutate: mockDelete, isPending: false }),
  };
});

const task = {
  id: "task/with space",
  project_id: "demo",
  title: "Delete me",
  status: "READY",
} as never;

describe("TaskActions deletion", () => {
  it("closes a pane and does not create a duplicate navigation entry when returnTo is current", async () => {
    const onDeleted = vi.fn();
    mockNavigate.mockReset();
    mockDelete.mockImplementation((_input, options) => options.onSuccess());

    render(
      <TaskActions
        task={task}
        returnTo="/command-center/graph?q=needle"
        onDeleted={onDeleted}
      />,
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete" }));

    expect(mockDelete).toHaveBeenCalledWith(
      { task_id: "task/with space" },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
    expect(onDeleted).toHaveBeenCalledOnce();
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});
