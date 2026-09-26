import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("../../../api/client", () => ({ reportGet: vi.fn() }));
import { reportGet } from "../../../api/client";
import FocusShell from "../FocusShell";
import FocusReport from "../FocusReport";

const record = {
  id: "morning-2026-09-25", state: "final", reason: null, local_date: "2026-09-25", timezone: "UTC",
  window_start: 0, window_end: 86400, is_fallback: false, author_deadline: 0,
  report: { summary: "Summary", projects: [{ id: "p", name: "P",
    landed: [{ refs: ["completion:c1"], text: "Landed", task_id: "stark-impact-60.1" }],
    pending: [], failures: [], manual_checks: [] }], coverage: { complete: true }, global_facts: [], omitted: {} },
};

describe("FocusReport", () => {
  it("renders the report inside the focus shell and keeps task links in focus", async () => {
    vi.mocked(reportGet).mockResolvedValue({ data: { report: record } } as never);
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/focus/reports/morning-2026-09-25"]}>
          <Routes>
            <Route path="/focus" element={<FocusShell />}>
              <Route path="reports/:reportId" element={<FocusReport />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Morning report · 2026-09-25")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Task stark-impact-60.1" })).toHaveAttribute("href", "/focus/tasks/stark-impact-60.1");
    expect(screen.getByRole("link", { name: "Open in full dashboard" })).toHaveAttribute("href", "/reports/morning-2026-09-25");
  });
});
