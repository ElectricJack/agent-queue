import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import ProjectSessions from "../Sessions";

// jsdom lays nothing out; give the virtualizer a 600px viewport to fill.
const originalOffsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
const originalOffsetWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, writable: true, value: 600 });
  Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, writable: true, value: 800 });
});
afterAll(() => {
  if (originalOffsetHeight) Object.defineProperty(HTMLElement.prototype, "offsetHeight", originalOffsetHeight);
  if (originalOffsetWidth) Object.defineProperty(HTMLElement.prototype, "offsetWidth", originalOffsetWidth);
});
afterEach(cleanup);

const mocks = vi.hoisted(() => ({ sessions: [] as unknown[], isLoading: false }));
vi.mock("../../../api/hooks", () => ({
  useSessions: () => ({ data: mocks.sessions, isLoading: mocks.isLoading }),
}));

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/projects/p1/sessions"]}>
      <Routes>
        <Route path="/projects/:projectId/sessions" element={<ProjectSessions />} />
      </Routes>
    </MemoryRouter>,
  );
}

const session = (i: number) => ({
  id: "s" + i, name: "session-" + i, task_id: "t" + i, harness: "claude",
  state: i === 1 ? "running" : "stopped", stalled: i === 1, idle_seconds: 12.4,
});

describe("ProjectSessions", () => {
  it("mounts only the rows in view for a long session history", () => {
    mocks.sessions = Array.from({ length: 3000 }, (_, i) => session(i));
    renderPage();
    const links = screen.getAllByRole("link");
    expect(links[0]).toHaveTextContent("session-0");
    expect(links[0]).toHaveAttribute("href", "/sessions/s0");
    // A 600px viewport of ~41px rows plus overscan, nowhere near 3,000.
    expect(links.length).toBeGreaterThan(10);
    expect(links.length).toBeLessThan(60);
    expect(screen.getByText("running")).toHaveClass("text-amber-400");
    expect(screen.getAllByText("12s").length).toBe(links.length);
  });

  it("says so when the project has no sessions", () => {
    mocks.sessions = [];
    renderPage();
    expect(screen.getByText("No sessions for this project.")).toBeInTheDocument();
  });
});
