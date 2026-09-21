import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import TaskActions from "../TaskActions";

const mockNavigate = vi.fn();
const mockDelete = vi.fn();
const mockSendChatMessage = vi.fn();
/** The delete mutation's reported failure; a test that cares sets its own. */
const GENERIC_FAILURE = new Error("A descendant still has a live session");
const deleteFailure = vi.hoisted(() => ({
  current: null as unknown,
}));

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
      // Opening the dialog clears any error left from a previous attempt.
      reset: vi.fn(),
      isPending: false,
      isError: true,
      error: deleteFailure.current,
    }),
  };
});

beforeEach(() => {
  deleteFailure.current = GENERIC_FAILURE;
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

  it("explains an integration-history refusal instead of offering another attempt", async () => {
    // The daemon refuses before any write because append-only integration
    // audit rows still name the subtree. Retrying cannot help and there is no
    // branch question to ask, so the dialog says why in plain words and stops
    // offering the button.
    deleteFailure.current = Object.assign(new Error("API 422"), {
      payload: {
        success: false,
        code: "hierarchy.integration_owned",
        error:
          "hierarchy.integration_owned: delete would orphan 1 integration record(s): " +
          "integration_batch_members(azure-beacon)",
        references: [{ task_id: "azure-beacon", table: "integration_batch_members" }],
      },
    });
    mockDelete.mockReset();

    render(<TaskActions task={task} />);
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));

    const dialog = screen.getByRole("dialog");
    const alert = within(dialog).getByRole("alert");
    expect(alert).toHaveTextContent(/permanent record, so it cannot be deleted/);
    expect(alert).toHaveTextContent(/once archiving tasks with integration history is supported/);
    expect(alert).not.toHaveTextContent(/hierarchy\.|integration_batch_members/);
    expect(
      within(dialog).getByRole("button", { name: "Delete task and descendants" }),
    ).toBeDisabled();
    // No branch question either.
    expect(within(dialog).queryByLabelText(/Delete the branch/)).not.toBeInTheDocument();
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

  it("asks about branches instead of reporting the refusal, then re-issues the delete", async () => {
    // The server refuses the first attempt because the subtree has a branch on
    // the remote, and names it.  That is a question, not a failure — the
    // dialog has to turn it into a choice rather than a red alert.
    mockDelete.mockReset();
    mockDelete.mockImplementationOnce((_input, options) =>
      options.onError(
        Object.assign(new Error("API 422"), {
          payload: {
            code: "hierarchy.branch_discard_required",
            branches: [
              { task_id: "azure-beacon", branch: "aq/azure-beacon", base_sha: "abc" },
            ],
          },
        }),
      ),
    );
    mockDelete.mockImplementationOnce((_input, options) => options.onSuccess());

    render(<TaskActions task={task} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Delete" }));

    const dialog = screen.getByRole("dialog");
    await user.click(
      within(dialog).getByRole("button", { name: "Delete task and descendants" }),
    );

    expect(mockDelete).toHaveBeenNthCalledWith(
      1,
      { task_id: "task/with space", cascade: true },
      expect.anything(),
    );
    expect(within(dialog).getByText("aq/azure-beacon")).toBeInTheDocument();

    // Keep is preselected: leaving a ref behind is recoverable, deleting one
    // is not.
    await user.click(within(dialog).getByLabelText(/Delete the branch/));
    await user.click(within(dialog).getByRole("button", { name: "Delete task and branches" }));

    expect(mockDelete).toHaveBeenNthCalledWith(
      2,
      { task_id: "task/with space", cascade: true, branches: "delete" },
      expect.anything(),
    );
  });

  it("keeps the branch when the operator leaves the default alone", async () => {
    mockDelete.mockReset();
    mockDelete.mockImplementationOnce((_input, options) =>
      options.onError(
        Object.assign(new Error("API 422"), {
          payload: { code: "hierarchy.branch_discard_required", branches: [] },
        }),
      ),
    );
    mockDelete.mockImplementationOnce((_input, options) => options.onSuccess());

    render(<TaskActions task={task} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Delete" }));

    const dialog = screen.getByRole("dialog");
    await user.click(
      within(dialog).getByRole("button", { name: "Delete task and descendants" }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Delete task and descendants" }),
    );

    expect(mockDelete).toHaveBeenNthCalledWith(
      2,
      { task_id: "task/with space", cascade: true, branches: "keep" },
      expect.anything(),
    );
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
