import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import TaskCollaboration from "../TaskCollaboration";
import TaskDetail from "../../pages/TaskDetail";
import TaskDetailPane from "../../panes/task-detail";

const api = vi.hoisted(() => ({
  collaborationList: vi.fn(), collaborationGet: vi.fn(), taskComments: vi.fn(),
}));
vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api/client")>(), ...api,
}));
vi.mock("../../api/hooks", () => ({
  useTask: (id: string) => ({ data: { id, project_id: "demo", title: `Task ${id}`, description: "", status: "IN_PROGRESS" } }),
  useProfiles: () => ({ data: [] }),
  useIntelligenceClasses: () => ({ data: { success: true, classes: [] } }),
  useEditTask: () => ({}), useGates: () => ({ data: [] }),
  useResolveGate: () => ({}), useDeleteTask: () => ({}), useReopenWithFeedback: () => ({}),
  useTaskAttachments: () => ({ data: { success: true, attachments: [] } }),
  useUploadTaskAttachment: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteTaskAttachment: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("../TaskActions", () => ({ default: () => null }));
vi.mock("../TaskSessions", () => ({ default: () => null }));
vi.mock("../TaskSubtaskList", () => ({ default: () => null }));
vi.mock("../../pages/task/TaskGraph", () => ({ default: () => null, TaskExplain: () => null }));
vi.mock("../../panes/store", () => ({ useShellPaneStore: () => ({ open: vi.fn(), close: vi.fn() }) }));

const member = (task_id: string, overrides: Record<string, unknown> = {}) => ({
  task_id, state: "accepted", invited_at: 1755878000, accepted_at: 1755878100, accepted_claim_epoch: 1,
  removed_at: null, task_status: "IN_PROGRESS", task_claim_epoch: 1, running: true, needs_accept: false,
  ...overrides,
});
const thread = (overrides: Record<string, unknown> = {}) => ({
  id: "collab-0123456789abcdef", project_id: "demo", created_by_kind: "supervisor",
  created_by_id: "supervisor-demo", idempotency_key: "pair-1", goal: "Agree on the claim schema",
  state: "active", close_reason: null, created_at: 1755878000, deadline_at: 1755885200, closed_at: null,
  remaining_seconds: 4320, message_budget: 40, message_count: 3, last_seq: 3, version: 2, final_result: null,
  members: [
    member("t1"),
    member("t2", { state: "invited", accepted_at: null, accepted_claim_epoch: null, task_status: "READY", running: false, needs_accept: true }),
    member("t3", { accepted_claim_epoch: 1, task_claim_epoch: 2, needs_accept: true }),
  ],
  ...overrides,
});
const message = (seq: number, body: string | null, sender_task_id = "t2") => ({
  seq, message_id: `msg-${seq}`, sender_task_id, subject: null, body, created_at: 1755878400 + seq * 60,
});
const listed = (threads: unknown[]) => ({ data: { success: true, threads, count: threads.length } });
const fetched = (record: ReturnType<typeof thread>, messages: unknown[], hasMore = false) => ({
  data: { success: true, thread: record, messages, next_cursor: 3, has_more: hasMore, capacity_hold: false, next_step: "" },
});

let client: QueryClient;
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  api.collaborationList.mockReset().mockResolvedValue(listed([]));
  api.collaborationGet.mockReset();
  api.taskComments.mockReset().mockResolvedValue({ data: { success: true, comments: [], total: 0, limit: 50, offset: 0 } });
});
afterEach(() => { cleanup(); client.clear(); });

function mountPanel() {
  return render(<QueryClientProvider client={client}><TaskCollaboration taskId="t1" /></QueryClientProvider>);
}
function serveThread(record: ReturnType<typeof thread>, messages: unknown[], hasMore = false) {
  api.collaborationList.mockResolvedValue(listed([record]));
  api.collaborationGet.mockResolvedValue(fetched(record, messages, hasMore));
}

describe("task collaboration panel", () => {
  it("renders nothing when the task has no collaboration threads", async () => {
    const { container } = mountPanel();
    await waitFor(() => expect(client.getQueryState(["task", "t1", "collaborations"])?.status).toBe("success"));
    expect(api.collaborationList).toHaveBeenCalledWith({ body: { task_id: "t1" }, throwOnError: true });
    expect(api.collaborationGet).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });

  it("shows state, goal, deadline, budget, members and messages in seq order", async () => {
    serveThread(thread(), [message(3, "Pushed the migration", "t1"), message(1, "Which column name?"), message(2, "claim_epoch")]);
    mountPanel();
    const panel = await screen.findByRole("region", { name: "Collaboration" });
    expect(api.collaborationGet).toHaveBeenCalledWith({ body: { thread_id: "collab-0123456789abcdef" }, throwOnError: true });
    expect(within(panel).getByText("active")).toBeInTheDocument();
    expect(within(panel).getByText("Agree on the claim schema")).toBeInTheDocument();
    expect(within(panel).getByText("Ends in 1h 12m")).toBeInTheDocument();
    expect(within(panel).getByText("3/40 messages")).toBeInTheDocument();

    const members = within(within(panel).getByRole("list", { name: "Members" })).getAllByRole("listitem");
    expect(members.map((row) => row.textContent)).toEqual([
      expect.stringMatching(/t1.*running.*IN PROGRESS.*accepted/),
      expect.stringMatching(/t2.*not running.*READY.*invited/),
      expect.stringMatching(/t3.*running.*IN PROGRESS.*needs accept/),
    ]);

    const log = await within(panel).findByRole("list", { name: "Messages" });
    const rows = within(log).getAllByRole("listitem");
    expect(rows.map((row) => row.querySelector("p")?.textContent)).toEqual(["Which column name?", "claim_epoch", "Pushed the migration"]);
    expect(rows[0]).toHaveTextContent("#1");
    expect(rows[0]).toHaveTextContent("t2");
    expect(rows[2]).toHaveTextContent("t1");
    expect(rows[0]?.querySelector("time[datetime]")).not.toBeNull();
    expect(within(panel).queryByText(/Older messages/)).toBeNull();
  });

  it("says when older messages are not shown and when a body has been retired", async () => {
    serveThread(thread(), [message(21, null)], true);
    mountPanel();
    expect(await screen.findByText("Older messages are not shown.")).toBeInTheDocument();
    expect(screen.getByText("Message content has expired.")).toBeInTheDocument();
  });

  it("shows a closed thread's close reason and final result", async () => {
    serveThread(thread({
      state: "closed", close_reason: "budget_exhausted", closed_at: 1755880000, remaining_seconds: 0,
      message_count: 40, last_seq: 40,
      final_result: { reason: "budget_exhausted", note: "Schema agreed; t1 owns the migration", message_count: 40, last_seq: 40 },
    }), [message(40, "Done")]);
    mountPanel();
    const panel = await screen.findByRole("region", { name: "Collaboration" });
    expect(within(panel).getByText("closed")).toBeInTheDocument();
    expect(within(panel).getByText("40/40 messages")).toBeInTheDocument();
    const result = within(panel).getByRole("group", { name: "Final result" });
    expect(result).toHaveTextContent("budget exhausted");
    expect(result).toHaveTextContent("Schema agreed; t1 owns the migration");
    expect(within(panel).queryByText(/Ends in/)).toBeNull();
  });

  it("names an active thread past its deadline as ended", async () => {
    serveThread(thread({ remaining_seconds: 0 }), []);
    mountPanel();
    expect(await screen.findByText("Deadline passed")).toBeInTheDocument();
    expect(await screen.findByText("No messages yet.")).toBeInTheDocument();
  });

  it("renders message bodies and goals as plain text, never HTML", async () => {
    const markup = '<img src=x onerror=alert(1)> <b>bold</b>';
    serveThread(thread({ goal: '<script>alert("goal")</script>' }), [message(1, markup)]);
    mountPanel();
    expect(await screen.findByText(markup)).toBeInTheDocument();
    expect(screen.getByText('<script>alert("goal")</script>')).toBeInTheDocument();
    expect(document.querySelector("img, b, script")).toBeNull();
  });

  it("reports a thread that could not be loaded", async () => {
    api.collaborationList.mockResolvedValue(listed([thread()]));
    api.collaborationGet.mockRejectedValue(new Error("API 404: Collaboration thread not found"));
    mountPanel();
    expect(await screen.findByRole("alert")).toHaveTextContent("Collaboration thread not found");
    expect(screen.getByText("Agree on the claim schema")).toBeInTheDocument();
  });

  it.each(["full", "drawer"] as const)("mounts beside the comments in %s task detail", async (surface) => {
    serveThread(thread(), [message(1, "Hello partner")]);
    const noop = () => {};
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/tasks/t1"]}>
      {surface === "full"
        ? <Routes><Route path="/tasks/:taskId" element={<TaskDetail />} /></Routes>
        : <TaskDetailPane args={{ taskId: "t1" }} close={noop} setArgs={noop} setToolbar={noop} setShortcuts={noop} />}
    </MemoryRouter></QueryClientProvider>);
    expect(await screen.findByText("Hello partner")).toBeInTheDocument();
    expect(screen.getAllByRole("region", { name: "Collaboration" })).toHaveLength(1);
    expect(api.collaborationList).toHaveBeenCalledWith({ body: { task_id: "t1" }, throwOnError: true });
  });
});
