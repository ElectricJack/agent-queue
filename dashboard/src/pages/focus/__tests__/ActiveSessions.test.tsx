import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import ActiveSessions from "../ActiveSessions";

vi.mock("../../../api/agents", () => ({
  useAgentFlock: () => ({
    isLoading: false, error: null,
    data: [
      { id: "a", name: "worker-a", session_id: "s-a", session_state: "running", session_provider: "tmux", current_task_title: "Fix it" },
      { id: "b", name: "worker-b", session_id: "s-b", session_state: "sleeping", session_provider: "tmux" },
      { id: "c", name: "worker-c", session_id: "s-c", session_state: "running", session_provider: "fake" },
    ],
  }),
}));
vi.mock("../../agents/pools", () => ({
  usePoolFlock: () => ({
    entries: [{ profileId: "deep-high-claude", pool: { profile_id: "deep-high-claude", name: "" }, instances: [
      { id: "p-1", name: "pool-1", state: "running", provider: "tmux", started_at: 1790000000 },
      { id: "s-a", name: "dup", state: "running", provider: "tmux", started_at: 1 },
    ] }],
  }),
  poolDisplayName: (pool: { name?: string; profile_id: string }) => pool.name || pool.profile_id,
}));

describe("ActiveSessions", () => {
  it("lists live tmux sessions once each, linking the phone terminal", () => {
    render(<MemoryRouter><ActiveSessions /></MemoryRouter>);
    const links = screen.getAllByRole("link");
    expect(links.map((l) => l.getAttribute("href"))).toEqual([
      "/focus/sessions/s-a",
      "/focus/sessions/p-1?started=1790000000",
    ]);
    expect(screen.getByText("Fix it")).toBeInTheDocument();
    expect(screen.queryByText("worker-b")).toBeNull();
    expect(screen.queryByText("worker-c")).toBeNull();
  });
});
