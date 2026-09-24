import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import ProjectSessions from "../Sessions";
import SystemSessions from "../../system/Sessions";

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

const api = vi.hoisted(() => ({ sessionList: vi.fn() }));
vi.mock("../../../api/client", () => api);

function Location() {
  const location = useLocation();
  return <><output aria-label="Location">{location.pathname + location.search}</output>
    <output aria-label="Terminal focus request">{String(location.state?.terminalFocus === true)}</output></>;
}

function renderPage(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/projects/:projectId/sessions" element={<ProjectSessions />} />
          <Route path="/sessions" element={<SystemSessions />} />
        </Routes>
        <Location />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}

const session = (i: number) => ({
  id: "s" + i, name: "session-" + i, task_id: "t" + i, harness: "claude",
  state: i === 1 ? "running" : "stopped", stalled: i === 1, idle_seconds: 12.4,
});

afterEach(() => cleanup());

describe("ProjectSessions", () => {
  beforeEach(() => {
    api.sessionList.mockReset();
  });

  it("mounts only the rows in view for a full page of session history", async () => {
    api.sessionList.mockResolvedValue({
      data: { sessions: Array.from({ length: 100 }, (_, i) => session(i)), count: 100, has_more: true },
    });
    const client = renderPage("/projects/p1/sessions");
    await screen.findByText("session-0");
    const links = screen.getAllByRole("link");
    expect(links[0]).toHaveTextContent("session-0");
    expect(links[0]).toHaveAttribute("href", "/sessions/s0");
    // A 600px viewport of ~41px rows plus overscan, not the whole page of 100.
    expect(links.length).toBeGreaterThan(10);
    expect(links.length).toBeLessThan(60);
    expect(screen.getByText("running")).toHaveClass("text-amber-400");
    expect(screen.getAllByText("12s").length).toBe(links.length);
    client.clear();
  });

  it.each(["/projects/p1/sessions", "/sessions"])("marks a session link from %s as an explicit terminal selection", async (path) => {
    api.sessionList.mockResolvedValue({ data: { sessions: [session(1)], count: 1, has_more: false } });
    const client = renderPage(path);
    fireEvent.click(await screen.findByRole("link", { name: "session-1" }));
    expect(screen.getByLabelText("Terminal focus request")).toHaveTextContent("true");
    client.clear();
  });

  it("starts the next page at the top of the table", async () => {
    api.sessionList.mockImplementation(async ({ body }) => ({
      data: {
        sessions: Array.from({ length: 100 }, (_, i) => session(body.offset + i)),
        count: 100,
        has_more: body.offset === 0,
      },
    }));
    const client = renderPage("/projects/p1/sessions");
    await screen.findByText("session-0");
    const scroller = screen.getByRole("table").parentElement!;
    scroller.scrollTop = 400;

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("session-100");
    expect(scroller.scrollTop).toBe(0);
    client.clear();
  });

  it("says so when the project has no sessions", async () => {
    api.sessionList.mockResolvedValue({ data: { sessions: [], count: 0, has_more: false } });
    const client = renderPage("/projects/p1/sessions");
    expect(await screen.findByText("No sessions for this project.")).toBeInTheDocument();
    client.clear();
  });
});

describe("Sessions pages", () => {
  beforeEach(() => {
    api.sessionList.mockReset();
    api.sessionList.mockImplementation(async ({ body }) => ({
      data: {
        sessions: [{ id: `session-${body.offset}`, name: `session-${body.offset}` }],
        count: 1,
        has_more: body.offset === 0,
      },
    }));
  });

  it.each([
    ["/projects/p1/sessions", "p1"],
    ["/sessions", undefined],
  ])("fetches only the visible page at %s", async (path, projectId) => {
    const client = renderPage(path);
    await screen.findByText("session-0");
    expect(api.sessionList).toHaveBeenCalledWith(expect.objectContaining({
      body: expect.objectContaining({ limit: 100, offset: 0, ...(projectId ? { project_id: projectId } : {}) }),
    }));
    if (!projectId) expect(api.sessionList.mock.calls[0]![0].body).not.toHaveProperty("project_id");

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("session-100");
    expect(api.sessionList).toHaveBeenLastCalledWith(expect.objectContaining({
      body: expect.objectContaining({ limit: 100, offset: 100, ...(projectId ? { project_id: projectId } : {}) }),
    }));
    if (!projectId) expect(api.sessionList.mock.lastCall?.[0].body).not.toHaveProperty("project_id");
    expect(screen.getByLabelText("Location")).toHaveTextContent(`${path}?page=2`);
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    await waitFor(() => expect(screen.getByLabelText("Location")).toHaveTextContent(path));
    client.clear();
  });
});
