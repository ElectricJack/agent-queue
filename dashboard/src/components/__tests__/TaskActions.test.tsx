import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import TaskActions from "../TaskActions";
import { TerminalLinkModeProvider } from "../terminalLinks";

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
    useDeleteTask: () => ({ mutateAsync: mockDelete, isPending: false }),
    useDeleteTaskPreview: () => ({ data: { task_ids: ["task/with space"], tasks: [],
      reparented_tasks: [], branch_details: [], sessions: [], warnings: [] }, isFetching: false }),
  };
});

beforeEach(() => {
  mockDelete.mockReset();
  mockDelete.mockResolvedValue({ warnings: [] });
});

const task = {
  id: "task/with space",
  project_id: "demo",
  title: "Delete me",
  status: "READY",
} as never;

describe("TaskActions deletion", () => {
  it("offers one Delete action even while running", async () => {
    render(<TaskActions task={{ ...(task as object), status: "IN_PROGRESS" } as never} />);
    expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(screen.getByLabelText("Deletion scope")).toHaveValue("task");
    expect(screen.getByLabelText("Branch cleanup")).toHaveValue("keep");
    await userEvent.click(screen.getByRole("button", { name: "Delete selected tasks" }));
    expect(mockDelete).toHaveBeenCalledOnce();
    expect(mockDelete).toHaveBeenCalledWith({ task_id: "task/with space", recursive: false,
      branches: "keep", stop_running: true, reason: "", confirmed: true });
  });

  it("closes a pane without duplicate navigation when returnTo is current", async () => {
    const onDeleted = vi.fn();
    mockNavigate.mockReset();
    render(<TaskActions task={task} returnTo="/command-center/graph?q=needle" onDeleted={onDeleted} />);
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete selected tasks" }));
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
      state: { agentSelection: "replace", terminalFocus: "supervisor-global" },
    });
  });

  it("in watch mode opens the supervisor's focus session, never /agents", async () => {
    mockNavigate.mockReset();
    mockSendChatMessage.mockReset();
    mockSendChatMessage.mockResolvedValue({ message_id: "m1" });

    render(<TerminalLinkModeProvider mode="watch"><TaskActions task={blocked} /></TerminalLinkModeProvider>);
    await userEvent.click(screen.getByRole("button", { name: "Ask supervisor why" }));

    expect(mockNavigate).toHaveBeenCalledWith("/focus/sessions/supervisor-global");
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
