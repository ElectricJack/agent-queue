import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import ProjectSessions from "../Sessions";
import SystemSessions from "../../system/Sessions";

const api = vi.hoisted(() => ({ sessionList: vi.fn() }));
vi.mock("../../../api/client", () => api);

function Location() {
  const location = useLocation();
  return <output aria-label="Location">{location.pathname + location.search}</output>;
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

  afterEach(() => cleanup());

  it.each([
    ["/projects/p1/sessions", "p1"],
    ["/sessions", undefined],
  ])("fetches only the visible page at %s", async (path, projectId) => {
    const client = renderPage(path);
    await screen.findByText("session-0");
    expect(api.sessionList).toHaveBeenCalledWith(expect.objectContaining({
      body: expect.objectContaining({ limit: 100, offset: 0, ...(projectId ? { project_id: projectId } : {}) }),
    }));
    if (!projectId) expect(api.sessionList.mock.calls[0][0].body).not.toHaveProperty("project_id");

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
