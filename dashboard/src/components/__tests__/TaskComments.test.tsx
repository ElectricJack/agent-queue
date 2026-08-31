import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { useState } from "react";
import TaskDetail from "../../pages/TaskDetail";
import TaskDetailPane from "../../panes/task-detail";

const fixtures = vi.hoisted(() => Object.fromEntries(["t1", "t2"].map((id) => [id, {
  id, project_id: "demo", title: `Task ${id}`, description: "Requirements", status: "IN_PROGRESS",
}])));
const api = vi.hoisted(() => ({ taskComment: vi.fn(), taskComments: vi.fn() }));
vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api/client")>(), ...api,
}));
vi.mock("../../api/hooks", () => ({
  useTask: (id: string) => ({ data: fixtures[id] }),
  useProjectProfiles: () => ({ data: { agent_types: [] } }),
  useEditTask: () => ({}), useGates: () => ({ data: [] }),
  useResolveGate: () => ({}), useDeleteTask: () => ({}), useReopenWithFeedback: () => ({}),
}));
vi.mock("../TaskActions", () => ({ default: () => null }));
vi.mock("../../pages/task/TaskGraph", () => ({ default: () => null, TaskExplain: () => null }));
vi.mock("../../panes/store", () => ({ useShellPaneStore: () => ({ open: vi.fn(), close: vi.fn() }) }));

const comment = (body: string, taskId = "t1") => ({
  id: `c-${body}`, task_id: taskId, body, author_kind: "agent", author_id: "worker-7", created_at: 1755878400,
});
const page = (comments = [comment("Verified the migration")], total = comments.length, offset = 0) => ({
  data: { success: true, comments, total, limit: 50, offset },
});
let client: QueryClient;
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  api.taskComments.mockReset().mockResolvedValue(page());
  api.taskComment.mockReset().mockImplementation(async ({ body }) => ({ data: { success: true, comment: comment(body.body, body.task_id) } }));
});
afterEach(() => { cleanup(); client.clear(); });
const noop = () => {};
function Pane({ taskId }: { taskId: string }) {
  return <TaskDetailPane args={{ taskId }} close={noop} setArgs={noop} setToolbar={noop} setShortcuts={noop} />;
}
function mount(surface: "full" | "drawer" = "drawer") {
  function Harness() {
    const [taskId, setTaskId] = useState("t1");
    return <><button onClick={() => setTaskId(taskId === "t1" ? "t2" : "t1")}>Switch task</button><Pane taskId={taskId} /></>;
  }
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/tasks/t1"]}>
    {surface === "full" ? <Routes><Route path="/tasks/:taskId" element={<TaskDetail />} /></Routes> : <Harness />}
  </MemoryRouter></QueryClientProvider>);
}
function write(body: string) { fireEvent.change(screen.getByRole("textbox", { name: "Add a comment" }), { target: { value: body } }); }
function submit() { fireEvent.click(screen.getByRole("button", { name: "Add comment" })); }

describe("task comments", () => {
  it.each(["full", "drawer"] as const)("shows history, author/time and a composer in %s detail", async (surface) => {
    mount(surface);
    expect(await screen.findByText("Verified the migration")).toBeInTheDocument();
    expect(screen.getByText("agent · worker-7")).toBeInTheDocument();
    expect(document.querySelector("time[datetime]")).not.toBeNull();
    expect(screen.getByRole("textbox", { name: "Add a comment" })).toBeInTheDocument();
  });
  it("renders comment content as escaped plain text", async () => {
    api.taskComments.mockResolvedValue(page([comment('<img src=x onerror=alert(1)> **findings**')]));
    mount();
    expect(await screen.findByText('<img src=x onerror=alert(1)> **findings**')).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
  });
  it("shows loading and an empty history, and rejects blank drafts", async () => {
    let resolve!: (value: ReturnType<typeof page>) => void;
    api.taskComments.mockReturnValue(new Promise((done) => { resolve = done; }));
    mount();
    expect(screen.getByText("Loading comments…")).toBeInTheDocument();
    await act(async () => { resolve(page([])); });
    expect(await screen.findByText("No comments yet.")).toBeInTheDocument();
    write("   ");
    expect(screen.getByRole("button", { name: "Add comment" })).toBeDisabled();
  });
  it("prevents overlong comments without losing the draft", () => {
    mount(); write("x".repeat(16_001));
    expect(screen.getByRole("alert")).toHaveTextContent("16,000 characters or fewer");
    expect(screen.getByRole("button", { name: "Add comment" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Add a comment" })).toHaveValue("x".repeat(16_001));
    expect(api.taskComment).not.toHaveBeenCalled();
  });
  it("never displays one task's history while a different task loads", async () => {
    mount();
    expect(await screen.findByText("Verified the migration")).toBeInTheDocument();
    api.taskComments.mockReturnValue(new Promise(() => {}));
    fireEvent.click(screen.getByRole("button", { name: "Switch task" }));
    expect(screen.getByText("Loading comments…")).toBeInTheDocument();
    expect(screen.queryByText("Verified the migration")).not.toBeInTheDocument();
  });
  it("prevents a duplicate submit if a pending task is left and reopened", async () => {
    let resolve!: (value: unknown) => void;
    api.taskComment.mockReturnValue(new Promise((done) => { resolve = done; }));
    mount(); write("in-flight finding"); submit();
    await waitFor(() => expect(api.taskComment).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("button", { name: "Switch task" }));
    fireEvent.click(screen.getByRole("button", { name: "Switch task" }));
    expect(screen.getByRole("button", { name: "Adding…" })).toBeDisabled();
    await act(async () => { resolve({ data: { success: true, comment: comment("in-flight finding") } }); });
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Add a comment" })).toHaveValue(""));
    expect(api.taskComment).toHaveBeenCalledOnce();
  });
  it("exposes load failure and retries without hiding the composer", async () => {
    api.taskComments.mockRejectedValueOnce(new Error("temporarily unavailable"));
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load comments");
    fireEvent.click(screen.getByRole("button", { name: "Retry comments" }));
    expect(await screen.findByText("Verified the migration")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Add a comment" })).toBeInTheDocument();
  });
  it("keeps a failed draft and clears it only after a successful comment append", async () => {
    api.taskComment.mockRejectedValueOnce(new Error("Permission denied"));
    mount();
    write("A useful finding"); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("Permission denied");
    expect(screen.getByRole("textbox", { name: "Add a comment" })).toHaveValue("A useful finding");
    api.taskComments.mockResolvedValue(page([comment("A useful finding")]));
    submit();
    expect(await screen.findByText("A useful finding")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Add a comment" })).toHaveValue(""));
    expect(api.taskComment).toHaveBeenLastCalledWith({ body: { task_id: "t1", body: "A useful finding" }, throwOnError: true });
  });
  it("preserves separate task drafts without submitting the previous task's text", async () => {
    mount(); write("t1 finding");
    fireEvent.click(screen.getByRole("button", { name: "Switch task" }));
    expect(screen.getByRole("textbox", { name: "Add a comment" })).toHaveValue("");
    expect(screen.getByRole("button", { name: "Add comment" })).toBeDisabled();
    write("t2 finding"); submit();
    await waitFor(() => expect(api.taskComment).toHaveBeenCalledWith({ body: { task_id: "t2", body: "t2 finding" }, throwOnError: true }));
    fireEvent.click(screen.getByRole("button", { name: "Switch task" }));
    expect(screen.getByRole("textbox", { name: "Add a comment" })).toHaveValue("t1 finding");
  });
  it("does not clear a new task's draft when a previous task submit completes", async () => {
    let resolve!: (value: unknown) => void;
    api.taskComment.mockReturnValue(new Promise((done) => { resolve = done; }));
    mount(); write("old task finding"); submit();
    await waitFor(() => expect(screen.getByRole("button", { name: "Adding…" })).toBeDisabled());
    fireEvent.click(screen.getByRole("button", { name: "Switch task" }));
    write("new task finding");
    await act(async () => { resolve({ data: { success: true, comment: comment("old task finding") } }); });
    expect(screen.getByRole("textbox", { name: "Add a comment" })).toHaveValue("new task finding");
    fireEvent.click(screen.getByRole("button", { name: "Switch task" }));
    expect(screen.getByRole("textbox", { name: "Add a comment" })).toHaveValue("");
  });
  it("retains edits made while an earlier draft is being submitted", async () => {
    let resolve!: (value: unknown) => void;
    api.taskComment.mockReturnValue(new Promise((done) => { resolve = done; }));
    mount(); write("first finding"); submit();
    await waitFor(() => expect(api.taskComment).toHaveBeenCalled());
    write("next finding");
    await act(async () => { resolve({ data: { success: true, comment: comment("first finding") } }); });
    expect(screen.getByRole("textbox", { name: "Add a comment" })).toHaveValue("next finding");
  });
  it("pages through older comments and returns to newest comments", async () => {
    api.taskComments.mockImplementation(async ({ body }) => body.offset === 50
      ? page([comment("Older finding")], 51, 50) : page([comment("Newest finding")], 51));
    mount();
    expect(await screen.findByText("Newest finding")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Older comments" }));
    expect(await screen.findByText("Older finding")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Older comments" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Newer comments" }));
    expect(await screen.findByText("Newest finding")).toBeInTheDocument();
  });
  it("refreshes comments when the existing task query prefix is invalidated", async () => {
    mount();
    expect(await screen.findByText("Verified the migration")).toBeInTheDocument();
    api.taskComments.mockResolvedValue(page([comment("Worker added a finding")]));
    await act(async () => { await client.invalidateQueries({ queryKey: ["task", "t1"] }); });
    expect(await screen.findByText("Worker added a finding")).toBeInTheDocument();
  });
});
