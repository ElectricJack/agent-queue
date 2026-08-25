import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import CommandCenterAgents from "../Agents";

const mockUsePaneStream = vi.fn();
const mockOpen = vi.fn();
const RUNNING = Array.from({ length: 10 }, (_, i) => ({
  id: `sid${i + 1}`,
  name: i === 0 ? "n-impl--t1" : `n-other-${i}`,
  task_id: i === 0 ? "t1" : `t${i + 1}`,
  state: "running",
}));

vi.mock("../../../ws/usePaneStream", () => ({
  usePaneStream: (...args: unknown[]) => mockUsePaneStream(...args),
}));
vi.mock("../../../api/hooks", () => ({
  useProjects: () => ({ data: [{ id: "p1", name: "P1" }] }),
  useAllAgents: () => ({
    data: [{ name: "n-impl", project_id: "p1", current_task_id: "t1", state: "busy" }],
    isLoading: false,
  }),
  useSessions: () => ({ data: RUNNING }),
}));
vi.mock("../../../panes/store", () => ({
  useShellPaneStore: () => ({ open: mockOpen }),
}));
vi.mock("../../../shell/hotkeys/useListNav", () => ({
  useListNav: () => ({ current: null }),
}));

beforeEach(() => {
  mockOpen.mockReset();
  mockUsePaneStream.mockReturnValue({
    screen: "AGENT SCREEN",
    status: "open",
    error: null,
    seq: 1,
  });
});

describe("CommandCenterAgents", () => {
  it("shows the table by default and does not subscribe", () => {
    render(<CommandCenterAgents />);
    expect(screen.getByText("n-impl")).toBeInTheDocument();
    expect(mockUsePaneStream).not.toHaveBeenCalled();
  });

  it("renders live tiles after switching to grid", () => {
    render(<CommandCenterAgents />);
    fireEvent.click(screen.getByRole("button", { name: /grid/i }));
    expect(screen.getAllByText(/AGENT SCREEN/).length).toBeGreaterThan(0);
  });

  it("opens the session-peek pane from a tile's header button", () => {
    render(<CommandCenterAgents />);
    fireEvent.click(screen.getByRole("button", { name: /grid/i }));
    fireEvent.click(screen.getByRole("button", { name: /open n-impl--t1/i }));
    expect(mockOpen).toHaveBeenCalledWith("session-peek", { sessionId: "sid1" });
  });

  it("caps live tiles below the backend cap and says how many are hidden", () => {
    render(<CommandCenterAgents />);
    fireEvent.click(screen.getByRole("button", { name: /grid/i }));
    // 8 tiles, not 12: a full grid must leave room under
    // pane_stream_max_sessions for a session detail page.
    const subscribed = new Set(
      mockUsePaneStream.mock.calls.map((c) => c[0] as string),
    );
    expect(subscribed.size).toBe(8);
    expect(
      screen.getByText(/\+2 more running sessions not shown/),
    ).toBeInTheDocument();
    expect(screen.getByText(/capped at 8/)).toBeInTheDocument();
  });

  it("marks the active view with aria-pressed, not colour alone", () => {
    render(<CommandCenterAgents />);
    expect(screen.getByRole("button", { name: /table/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: /grid/i })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    fireEvent.click(screen.getByRole("button", { name: /grid/i }));
    expect(screen.getByRole("button", { name: /grid/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("does not wrap the scrollable console in a button", () => {
    // <button> takes phrasing content only, and an overflow-auto console
    // inside one fires onOpen when you scroll or select text in it.
    render(<CommandCenterAgents />);
    fireEvent.click(screen.getByRole("button", { name: /grid/i }));
    const consoleEl = screen.getAllByText(/AGENT SCREEN/)[0].closest("div");
    expect(consoleEl?.closest("button")).toBeNull();
  });
});
