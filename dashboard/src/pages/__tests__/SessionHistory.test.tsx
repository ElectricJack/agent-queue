import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import SessionDetail from "../SessionDetail";
import { client } from "../../api/client";

const state = vi.hoisted(() => ({
  currentMissing: false,
  session: { id: "session-a", name: "Current Worker", project_id: "p2", task_id: "task-a", state: "running", provider: "tmux", started_at: 200 },
  transcript: { entries: [] as { _idx: number; type: string; text: string }[], status: "open", error: null as string | null, unavailable: null as string | null, clear: vi.fn() },
}));
vi.mock("../../api/hooks", () => ({
  useSession: () => ({ data: state.currentMissing ? undefined : state.session, isError: state.currentMissing }),
  useSessionAttach: () => ({ data: { attach_command: "tmux attach" } }),
  useSessionNudge: () => ({ mutate: vi.fn() }), useSessionKill: () => ({ mutate: vi.fn() }),
}));
vi.mock("../../ws/useTranscriptStream", () => ({ useTranscriptStream: () => state.transcript }));
vi.mock("../../components/InteractiveTerminal", () => ({ default: () => <div>Live interactive terminal</div> }));
const oldAttempt = { id: "old-attempt", session_id: "session-a", task_id: "task-a", agent_id: "agent-old", agent_name: "Historical Worker", model: "gpt-old", intelligence_class: "standard", harness: "codex", provider: "tmux", state: "stopped", work_dir: "/old", started_at: 100, session_started_at: 100, ended_at: 150, end_reason: "Worker exited", outcome: "failed", session_key: "old-key" };
let queryClient: QueryClient;
let request: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  request = vi.spyOn(client, "get").mockResolvedValue({ data: { task_id: "task-a", sessions: [oldAttempt] } } as never);
  state.currentMissing = false;
  state.session.started_at = 200;
  state.transcript.entries = [{ _idx: 0, type: "assistant", text: "Saved attempt output" }];
  state.transcript.error = null; state.transcript.unavailable = null;
});
afterEach(() => { cleanup(); queryClient.clear(); vi.restoreAllMocks(); });
function Origin() { const location = useLocation(); return <><p>Returned to {location.pathname + location.search + location.hash}</p><output aria-label="Restore task pane">{JSON.stringify(location.state)}</output></>; }
function mount(search = "?attempt=old-attempt&taskId=task-a", taskPane?: { taskId: string }) {
  return render(<QueryClientProvider client={queryClient}><MemoryRouter initialEntries={[{ pathname: "/sessions/session-a", search, state: { from: "/work/tasks?task=task-a#board", taskPane } }]}>
    <Routes><Route path="/sessions/:sessionId" element={<SessionDetail />} /><Route path="/work/tasks" element={<Origin />} /></Routes>
  </MemoryRouter></QueryClientProvider>);
}
function expectReadOnly() {
  expect(screen.queryByRole("heading", { name: "Attach" })).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Nudge" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Kill" })).not.toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: "Pane" })).not.toBeInTheDocument();
  expect(screen.queryByText("Live interactive terminal")).not.toBeInTheDocument();
}
describe("selected session attempts", () => {
  it("uses the ended attempt snapshot while the same session runs a newer attempt", async () => {
    mount();
    expect(await screen.findByRole("heading", { name: "Historical Worker" })).toBeInTheDocument();
    expect(screen.getByText("gpt-old")).toBeInTheDocument();
    expect(screen.getByText("Worker exited")).toBeInTheDocument();
    expect(screen.getByText("Saved attempt output")).toBeInTheDocument();
    expect(screen.queryByText("Current Worker")).not.toBeInTheDocument();
    expectReadOnly();
    fireEvent.click(screen.getByRole("link", { name: "Back to task" }));
    expect(screen.getByText("Returned to /work/tasks?task=task-a#board")).toBeInTheDocument();
  });
  it("carries the originating task pane back without changing the exact return URL", async () => {
    mount(undefined, { taskId: "task-a" });
    fireEvent.click(await screen.findByRole("link", { name: "Back to task" }));
    expect(screen.getByText("Returned to /work/tasks?task=task-a#board")).toBeInTheDocument();
    expect(screen.getByLabelText("Restore task pane")).toHaveTextContent('{"restoreTaskPane":{"taskId":"task-a"}}');
  });
  it("does not use the current session when the selected attempt is missing", async () => {
    request.mockResolvedValue({ data: { task_id: "task-a", sessions: [] } });
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("Session attempt not found");
    expect(screen.queryByText("Saved attempt output")).not.toBeInTheDocument();
    expectReadOnly();
  });
  it("does not use the current session when history cannot be loaded", async () => {
    request.mockRejectedValue(new Error("History denied"));
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load session attempt");
    expect(screen.queryByText("Saved attempt output")).not.toBeInTheDocument();
    expectReadOnly();
  });
  it("requires task context for an attempt link", async () => {
    mount("?attempt=old-attempt");
    expect(await screen.findByRole("alert")).toHaveTextContent("Task context is required");
    expectReadOnly();
  });
  it("shows an unavailable transcript without claiming to wait for live output", async () => {
    state.transcript.entries = [];
    state.transcript.unavailable = "Transcript file is not available for this attempt.";
    mount();
    expect(await screen.findByText("Transcript file is not available for this attempt.")).toBeInTheDocument();
    expect(screen.queryByText("Waiting for output…")).not.toBeInTheDocument();
    expectReadOnly();
  });
  it("keeps an unended old attempt read-only when the worker has restarted", async () => {
    request.mockResolvedValue({ data: { task_id: "task-a", sessions: [{ ...oldAttempt, ended_at: null, state: "running" }] } });
    mount();
    await screen.findByRole("heading", { name: "Historical Worker" });
    expectReadOnly();
  });
  it("still shows recorded history when the original session row no longer exists", async () => {
    state.currentMissing = true;
    mount();
    expect(await screen.findByRole("heading", { name: "Historical Worker" })).toBeInTheDocument();
    expect(screen.getByText("Saved attempt output")).toBeInTheDocument();
    expectReadOnly();
  });
  it("uses the pool process launch snapshot rather than the later task claim time", async () => {
    state.session.started_at = 100;
    request.mockResolvedValue({ data: { task_id: "task-a", sessions: [{ ...oldAttempt, started_at: 300, ended_at: null, state: "running" }] } });
    mount();
    expect(await screen.findByRole("tab", { name: "Pane" })).toBeInTheDocument();
  });
  it("allows the shared terminal for a matching current attempt", async () => {
    state.session.started_at = 100;
    request.mockResolvedValue({ data: { task_id: "task-a", sessions: [{ ...oldAttempt, ended_at: null, state: "running" }] } });
    mount();
    fireEvent.click(await screen.findByRole("tab", { name: "Pane" }));
    expect(screen.getByText("Live interactive terminal")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Kill" })).toBeInTheDocument();
  });
});
