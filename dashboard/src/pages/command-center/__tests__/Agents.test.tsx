import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import CommandCenterAgents from "../Agents";

const mockUsePaneStream = vi.fn();
const mockOpen = vi.fn();

vi.mock("../../../ws/usePaneStream", () => ({
  usePaneStream: (...args: unknown[]) => mockUsePaneStream(...args),
}));
vi.mock("../../../api/hooks", () => ({
  useProjects: () => ({ data: [{ id: "p1", name: "P1" }] }),
  useAllAgents: () => ({
    data: [{ name: "n-impl", project_id: "p1", current_task_id: "t1", state: "busy" }],
    isLoading: false,
  }),
  useSessions: () => ({
    data: [{ id: "sid1", name: "n-impl--t1", task_id: "t1", state: "running" }],
  }),
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
    expect(screen.getByText(/AGENT SCREEN/)).toBeInTheDocument();
  });

  it("opens the session-peek pane when a tile is clicked", () => {
    render(<CommandCenterAgents />);
    fireEvent.click(screen.getByRole("button", { name: /grid/i }));
    fireEvent.click(screen.getByRole("button", { name: /open n-impl/i }));
    expect(mockOpen).toHaveBeenCalledWith("session-peek", { sessionId: "sid1" });
  });
});
