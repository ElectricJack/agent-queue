import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import CreateTaskModal from "../CreateTaskModal";

const mutate = vi.hoisted(() => vi.fn());
vi.mock("../../api/hooks", () => ({
  useProjects: () => ({ data: [{ id: "agent-queue", name: "Agent Queue" }] }),
  useCreateTask: () => ({ mutate, isPending: false, error: null }),
  useIntelligenceClasses: () => ({
    data: {
      success: true,
      classes: [
        { id: "fast-low", name: "fast-low", description: "quick edits", revision: "", mapping: {} },
        { id: "deep-high", name: "deep-high", description: "hard problems", revision: "", mapping: {} },
      ],
    },
    isLoading: false,
    error: null,
  }),
}));

describe("CreateTaskModal", () => {
  it("requires an intelligence class and sends it with the task", async () => {
    render(<CreateTaskModal open onClose={vi.fn()} defaultProjectId="agent-queue" />);

    await userEvent.type(screen.getByLabelText("Title *"), "Copy task id button");
    const submit = screen.getByRole("button", { name: "Create Task" });
    expect(submit).toBeDisabled();

    await userEvent.selectOptions(screen.getByLabelText("Intelligence class *"), "fast-low");
    expect(submit).toBeEnabled();
    await userEvent.click(submit);

    expect(mutate).toHaveBeenCalledOnce();
    expect(mutate.mock.calls[0]![0]).toEqual({
      title: "Copy task id button",
      project_id: "agent-queue",
      intelligence_class: "fast-low",
    });
  });
});
