import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { testQueryClient } from "../../../testUtils/dashboardState";
import FocusShell from "../FocusShell";
import FocusSession from "../FocusSession";

type Session = { id: string; name: string; state: string; provider?: string; started_at?: number;
  ended_at?: number; end_reason?: string; agent_id?: string; task_id?: string; sleep_reason?: string };
const data = vi.hoisted(() => ({
  session: null as null | { data?: Session; isPending: boolean; isError: boolean; error: Error | null; refetch: () => void },
  flock: [] as { id: string; name: string; session_id?: string; session_state?: string }[],
}));
vi.mock("../../../api/hooks", () => ({ useSession: () => data.session }));
vi.mock("../../../api/agents", () => ({ useAgentFlock: () => ({ data: data.flock }) }));
vi.mock("../../../components/InteractiveTerminal", () => ({ default: ({ sessionId }: { sessionId: string }) => <p>Terminal {sessionId}</p> }));

const live = (over: Partial<Session> = {}) => ({
  data: { id: "s1", name: "worker-a", state: "running", provider: "tmux", started_at: 100, ...over },
  isPending: false, isError: false, error: null, refetch: vi.fn(),
});

function Location() {
  const l = useLocation();
  return <output aria-label="Current location">{l.pathname}{l.search}</output>;
}

function renderAt(path: string) {
  const client = testQueryClient();
  const tree = () => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/focus" element={<FocusShell />}>
            <Route path="sessions/:sessionId" element={<FocusSession />} />
          </Route>
        </Routes>
        <Location />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(tree());
  // Re-render a fresh copy of the tree (an identical element would bail out):
  // the router keeps its entry, and the mocked query hook returns whatever
  // `data.session` holds now — a refetch, in effect.
  return { ...view, again: () => view.rerender(tree()) };
}

beforeEach(() => {
  data.session = live();
  data.flock = [];
});

describe("FocusSession", () => {
  it("opens the phone terminal on a live tmux session", () => {
    renderAt("/focus/sessions/s1");
    expect(screen.getByText("Terminal s1")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("worker-a");
    expect(screen.getByRole("link", { name: "Open in full dashboard" })).toHaveAttribute("href", "/sessions/s1");
  });

  it("shows a restart notice instead of following a different process", async () => {
    data.session = live({ started_at: 200 });
    renderAt("/focus/sessions/s1?started=100");
    expect(screen.queryByText("Terminal s1")).toBeNull();
    expect(screen.getByText(/restarted/i)).toBeInTheDocument();
    act(() => screen.getByRole("button", { name: "Watch the new process" }).click());
    await waitFor(() => expect(screen.getByLabelText("Current location")).toHaveTextContent("/focus/sessions/s1?started=200"));
    expect(screen.getByText("Terminal s1")).toBeInTheDocument();
  });

  it("a pinned link to the current process watches it", () => {
    renderAt("/focus/sessions/s1?started=100");
    expect(screen.getByText("Terminal s1")).toBeInTheDocument();
  });

  it("an unpinned view pins the first process it saw", () => {
    const view = renderAt("/focus/sessions/s1");
    expect(screen.getByText("Terminal s1")).toBeInTheDocument();
    data.session = live({ started_at: 999 });
    view.again();
    expect(screen.queryByText("Terminal s1")).toBeNull();
    expect(screen.getByText(/restarted/i)).toBeInTheDocument();
  });

  it("an ended session shows its end and links the agent's current session", () => {
    data.session = live({ id: "old", state: "stopped", ended_at: 150, end_reason: "Task closed: pass", agent_id: "worker-a", task_id: "t-1" });
    data.flock = [{ id: "worker-a", name: "worker-a", session_id: "s2", session_state: "running" }];
    renderAt("/focus/sessions/old");
    expect(screen.queryByText("Terminal old")).toBeNull();
    expect(screen.getByText(/Session ended/)).toBeInTheDocument();
    expect(screen.getByText("Task closed: pass")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Watch worker-a's current session" })).toHaveAttribute("href", "/focus/sessions/s2");
    expect(screen.getByRole("link", { name: "Task t-1" })).toHaveAttribute("href", "/focus/tasks/t-1");
  });

  it("an ended session whose agent has no live session says so", () => {
    data.session = live({ id: "old", state: "stopped", agent_id: "worker-a" });
    data.flock = [{ id: "worker-a", name: "worker-a", session_id: "old", session_state: "stopped" }];
    renderAt("/focus/sessions/old");
    expect(screen.queryByRole("link", { name: /current session/ })).toBeNull();
    expect(screen.getByText(/No current session/)).toBeInTheDocument();
  });

  it("a starting session waits for its terminal", () => {
    data.session = live({ state: "starting" });
    renderAt("/focus/sessions/s1");
    expect(screen.queryByText("Terminal s1")).toBeNull();
    expect(screen.getByText(/is starting/)).toBeInTheDocument();
    expect(screen.queryByText(/Session ended/)).toBeNull();
  });

  it("a sleeping session says so, not that it ended", () => {
    data.session = live({ state: "sleeping", sleep_reason: "rate_limit" });
    renderAt("/focus/sessions/s1");
    expect(screen.getByText("Session asleep (rate_limit).")).toBeInTheDocument();
    expect(screen.queryByText(/Session ended/)).toBeNull();
  });

  it("a missing session is a scoped error", () => {
    data.session = { data: undefined, isPending: false, isError: true, error: new Error("No session 'gone'"), refetch: vi.fn() };
    renderAt("/focus/sessions/gone");
    expect(screen.getByRole("alert")).toHaveTextContent("No session 'gone'");
    expect(screen.getByRole("button", { name: "Go back" })).toBeInTheDocument();
  });

  it("a non-tmux session says there is no terminal view", () => {
    data.session = live({ provider: "fake" });
    renderAt("/focus/sessions/s1");
    expect(screen.queryByText("Terminal s1")).toBeNull();
    expect(screen.getByText(/no terminal view/i)).toBeInTheDocument();
  });
});
