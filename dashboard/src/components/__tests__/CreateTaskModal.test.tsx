import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CreateTaskModal from "../CreateTaskModal";

const mutate = vi.hoisted(() => vi.fn());
vi.mock("../../api/hooks", () => ({
  useProjects: () => ({ data: [{ id: "agent-queue", name: "Agent Queue" }] }),
  useCreateTask: () => ({ mutate, isPending: false, error: null }),
  useProfiles: () => ({ data: [{ id: "standard-high-codex", name: "standard-high-codex" }] }),
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

beforeEach(() => mutate.mockReset());

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

  it("offers Pin to this provider, unchecked and disabled until a profile is chosen", async () => {
    render(<CreateTaskModal open onClose={vi.fn()} defaultProjectId="agent-queue" />);
    const pin = screen.getByRole("checkbox", { name: "Pin to this provider" });
    expect(pin).not.toBeChecked();
    expect(pin).toBeDisabled();

    await userEvent.type(screen.getByLabelText("Title *"), "Pinned work");
    await userEvent.selectOptions(screen.getByLabelText("Intelligence class *"), "fast-low");
    await userEvent.selectOptions(screen.getByLabelText("Profile"), "standard-high-codex");
    expect(pin).toBeEnabled();
    expect(pin).not.toBeChecked();
    await userEvent.click(pin);
    await userEvent.click(screen.getByRole("button", { name: "Create Task" }));

    expect(mutate.mock.calls[0]![0]).toEqual({
      title: "Pinned work",
      project_id: "agent-queue",
      intelligence_class: "fast-low",
      profile_id: "standard-high-codex",
      pin: true,
    });
  });

  it("sends a chosen profile without pin when the box stays unchecked", async () => {
    render(<CreateTaskModal open onClose={vi.fn()} defaultProjectId="agent-queue" />);
    await userEvent.type(screen.getByLabelText("Title *"), "Preferred work");
    await userEvent.selectOptions(screen.getByLabelText("Intelligence class *"), "fast-low");
    await userEvent.selectOptions(screen.getByLabelText("Profile"), "standard-high-codex");
    await userEvent.click(screen.getByRole("button", { name: "Create Task" }));

    const body = mutate.mock.calls[0]![0];
    expect(body.profile_id).toBe("standard-high-codex");
    expect(body).not.toHaveProperty("pin");
    expect(body).not.toHaveProperty("provider_intent");
  });
});
