import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("../../../api/client", () => ({ reportGet: vi.fn() }));
import { reportGet } from "../../../api/client";
import MorningReportPage from "../MorningReportPage";

const record = {
  id: "morning-2026-09-25", state: "final", reason: "author_deadline", local_date: "2026-09-25",
  timezone: "America/Los_Angeles", window_start: 86400, window_end: 172800, is_fallback: true,
  report: {
    summary: "Recorded changes", projects: [{
      id: "p", name: "Project P",
      landed: [{ refs: ["completion:c1"], text: "A landed dashboard change", task_id: "t1", prior_verification: "42 tests passed" }],
      pending: [{ refs: ["completion:c2"], text: "Unmerged PR" }], failures: [], manual_checks: [],
    }],
    coverage: { complete: false, gaps: [{ source: "completions", reason: "read_failed" }],
      warnings: ["late_arrivals_outside_72h_unsupported"], window: { omitted_interval: { since: 0, until: 86400 } } },
    global_facts: [], omitted: {},
  },
};

function open() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={["/reports/morning-2026-09-25"]}>
      <Routes><Route path="/reports/:reportId" element={<MorningReportPage />} /></Routes>
    </MemoryRouter>
  </QueryClientProvider>);
}

beforeEach(() => vi.clearAllMocks());

describe("Morning report route", () => {
  it("reads the route id through the SDK and separates landing, verification and gaps", async () => {
    vi.mocked(reportGet).mockResolvedValue({ data: { report: record } } as never);
    open();
    expect(await screen.findByText("Morning report · 2026-09-25")).toBeInTheDocument();
    expect(reportGet).toHaveBeenCalledWith({ body: { report_id: "morning-2026-09-25" } });
    expect(screen.getByText("A landed dashboard change")).toBeInTheDocument();
    expect(screen.getByText("Unmerged PR")).toBeInTheDocument();
    expect(screen.getByText("Agent-reported verification: 42 tests passed")).toBeInTheDocument();
    expect(screen.getByText("Partial coverage")).toBeInTheDocument();
    expect(screen.getByText("completions: read_failed")).toBeInTheDocument();
    expect(screen.getByText(/Lookback capped/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Task t1" })).toHaveAttribute("href", "/tasks/t1");
  });

  it("renders skipped records without pretending coverage advanced", async () => {
    vi.mocked(reportGet).mockResolvedValue({ data: { report: { ...record, state: "skipped", reason: "late_start", report: null } } } as never);
    open();
    expect(await screen.findByText("This report was skipped. Coverage did not advance.")).toBeInTheDocument();
  });

  it("shows a readable failure for a missing or inaccessible report", async () => {
    vi.mocked(reportGet).mockRejectedValue(new Error("report not found"));
    open();
    expect(await screen.findByRole("alert")).toHaveTextContent("report not found");
  });
});
