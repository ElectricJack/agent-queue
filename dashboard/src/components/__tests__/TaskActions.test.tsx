import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import TaskActions from "../TaskActions";

const mockNavigate = vi.fn();
const mockDelete = vi.fn();
const mockSendChatMessage = vi.fn();

vi.mock("../../api/chat", () => ({
  sendChatMessage: (...args: unknown[]) => mockSendChatMessage(...args),
}));

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
    useReopenWithFeedback: mutation,
    useProvideInput: mutation,
    useDeleteTask: () => ({
      mutate: mockDelete,
      isPending: false,
      isError: true,
      error: new Error("A descendant still has a live session"),
    }),
  };
});

const task = {
  id: "task/with space",
  project_id: "demo",
  title: "Delete me",
  status: "READY",
} as never;

describe("TaskActions deletion", () => {
  it("shows a cascade deletion failure in the confirmation dialog", async () => {
    render(<TaskActions task={task} />);

    await userEvent.click(screen.getByRole("button", { name: "Delete" }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "A descendant still has a live session",
    );
  });

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
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Delete task and descendants",
      }),
    );

    expect(mockDelete).toHaveBeenCalledWith(
      { task_id: "task/with space", cascade: true },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
    expect(onDeleted).toHaveBeenCalledOnce();
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});

describe("TaskActions ask-supervisor", () => {
  const blocked = { ...(task as object), status: "BLOCKED" } as never;

  it("asks the global supervisor why a blocked task is stuck and switches to its terminal", async () => {
    mockNavigate.mockReset();
    mockSendChatMessage.mockReset();
    mockSendChatMessage.mockResolvedValue({ message_id: "m1" });

    render(<TaskActions task={blocked} />);
    await userEvent.click(screen.getByRole("button", { name: "Ask supervisor why" }));

    expect(mockSendChatMessage).toHaveBeenCalledWith(
      "",
      expect.stringContaining("task/with space"),
      { sessionAddress: "supervisor-global", threadId: "dashboard:global" },
    );
    expect(mockNavigate).toHaveBeenCalledWith("/agents?agent=supervisor-global", {
      state: { agentSelection: "replace" },
    });
  });

  it("is not offered for a task that is not blocked", () => {
    render(<TaskActions task={task} />);
    expect(screen.queryByRole("button", { name: "Ask supervisor why" })).toBeNull();
  });

  it("reports a failure to reach the supervisor and stays put", async () => {
    mockNavigate.mockReset();
    mockSendChatMessage.mockReset();
    mockSendChatMessage.mockRejectedValue(new Error("supervisor session unavailable"));

    render(<TaskActions task={blocked} />);
    await userEvent.click(screen.getByRole("button", { name: "Ask supervisor why" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("supervisor session unavailable");
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});
