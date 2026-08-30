import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import CommandCenterAgents from "../Agents";

const mockUsePaneStream = vi.fn();
const mockOpen = vi.fn();
const AGENTS = Array.from({ length: 10 }, (_, i) => ({
  id: "agent" + (i + 1),
  name: i === 0 ? "n-impl" : "worker-" + i,
  profile_id: "implementer",
  state: "busy",
  enabled: true,
  current_project_id: "p1",
  project_id: "p1",
  current_task_id: "t" + (i + 1),
  current_task_title: "Task " + (i + 1),
  session_id: "sid" + (i + 1),
  session_state: "running",
  session_provider: "tmux",
  provider: "anthropic",
  model: "claude-sonnet-4-6",
  intelligence_class: "standard-high",
  settings: { name: "worker-" + i, profile_id: "implementer", enabled: true },
}));
const RUNNING = AGENTS.map((agent) => ({
  id: agent.session_id, name: agent.name + "--" + agent.current_task_id,
  task_id: agent.current_task_id, state: "running",
}));

vi.mock("../../../ws/usePaneStream", () => ({
  usePaneStream: (...args: unknown[]) => mockUsePaneStream(...args),
}));
vi.mock("../../../api/agents", () => ({
  useAgentFlock: () => ({ data: AGENTS, isLoading: false }),
}));
vi.mock("../../../api/hooks", () => ({
  useProjects: () => ({ data: [{ id: "p1", name: "P1" }] }),
  useAllAgents: () => ({ data: AGENTS, isLoading: false }),
  useSessions: () => ({ data: RUNNING }),
}));
vi.mock("../../../panes/store", () => ({
  useShellPaneStore: () => ({ open: mockOpen }),
}));
vi.mock("../../../shell/hotkeys/useListNav", () => ({
  useListNav: () => ({ current: null }),
}));

function Location() {
  const location = useLocation();
  return <output aria-label="Location">{location.pathname}{location.search}</output>;
}

function renderAgents() {
  render(<MemoryRouter initialEntries={["/command-center/agents"]}><CommandCenterAgents /><Location /></MemoryRouter>);
}

beforeEach(() => {
  AGENTS.forEach((agent) => { agent.enabled = true; agent.session_state = "running"; });
  mockOpen.mockReset();
  mockUsePaneStream.mockReset();
  mockUsePaneStream.mockReturnValue({
    screen: "AGENT SCREEN",
    status: "open",
    error: null,
    seq: 1,
  });
});

describe("CommandCenterAgents", () => {
  it("shows the global table by default and does not subscribe", () => {
    renderAgents();
    expect(screen.getByText("n-impl")).toBeInTheDocument();
    expect(mockUsePaneStream).not.toHaveBeenCalled();
  });

  it("opens a global agent's workspace from its row", () => {
    renderAgents();
    fireEvent.click(screen.getByText("n-impl"));
    expect(screen.getByLabelText("Location")).toHaveTextContent("/agents?agent=agent1");
    expect(mockOpen).not.toHaveBeenCalled();
  });

  it("renders exact live session tiles after switching to grid", () => {
    renderAgents();
    fireEvent.click(screen.getByRole("button", { name: /grid/i }));
    expect(screen.getAllByText(/AGENT SCREEN/).length).toBeGreaterThan(0);
    expect(mockUsePaneStream.mock.calls[0]![0]).toBe("sid1");
  });

  it("opens the agent workspace from a tile's header button", () => {
    renderAgents();
    fireEvent.click(screen.getByRole("button", { name: /grid/i }));
    fireEvent.click(screen.getByRole("button", { name: /^open n-impl$/i }));
    expect(screen.getByLabelText("Location")).toHaveTextContent("/agents?agent=agent1");
  });

  it("caps the main view at four live tiles and reports the hidden workers", () => {
    renderAgents();
    fireEvent.click(screen.getByRole("button", { name: /grid/i }));
    const subscribed = new Set(mockUsePaneStream.mock.calls.map((c) => c[0] as string));
    expect(subscribed.size).toBe(4);
    expect(screen.getByText(/\+6 more running agents not shown/)).toBeInTheDocument();
  });

  it("marks the active view with aria-pressed, not colour alone", () => {
    renderAgents();
    expect(screen.getByRole("button", { name: /table/i })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /grid/i })).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(screen.getByRole("button", { name: /grid/i }));
    expect(screen.getByRole("button", { name: /grid/i })).toHaveAttribute("aria-pressed", "true");
  });

  it("does not wrap the scrollable console in a button", () => {
    renderAgents();
    fireEvent.click(screen.getByRole("button", { name: /grid/i }));
    for (const consoleEl of screen.getAllByText(/AGENT SCREEN/)) {
      expect(consoleEl.closest("button")).toBeNull();
    }
  });
});

describe("Command center current work", () => {
  it("keeps draining tmux sessions in the live grid", () => {
    AGENTS[0]!.session_state = "draining";
    renderAgents();
    fireEvent.click(screen.getByRole("button", { name: /grid/i }));
    expect(mockUsePaneStream.mock.calls.some((call) => call[0] === "sid1")).toBe(true);
  });

  it("shows busy separately from disabled eligibility for new work", () => {
    AGENTS[0]!.enabled = false;
    renderAgents();
    const row = screen.getByRole("row", { name: /n-impl/ });
    expect(within(row).getByText("busy")).toBeInTheDocument();
    expect(within(row).getByText("New work disabled")).toBeInTheDocument();
  });
});
