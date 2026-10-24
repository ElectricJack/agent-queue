import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import PlanPage from "../PlanPage";

const loadPlan = vi.hoisted(() => vi.fn());
vi.mock("../../api/client", () => ({ getPlan: loadPlan }));

function renderPlan(taskId = "solid-grove.13") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/plans/${taskId}`]}>
        <Link to="/plans/solid-grove.14">Next plan</Link>
        <Routes><Route path="/plans/:taskId" element={<PlanPage />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);

describe("PlanPage", () => {
  it("loads hierarchical IDs through the SDK and renders markdown safely", async () => {
    loadPlan.mockResolvedValue({ data: {
      task_id: "solid-grove.13",
      content: "# Implementation\n\n**Keep & display** <script>alert('x')</script>\n\n[Unsafe](javascript:alert(1))",
    } });
    const { container } = renderPlan();
    expect(await screen.findByRole("heading", { name: "Implementation" })).toBeInTheDocument();
    expect(screen.getByText("Keep & display").tagName).toBe("STRONG");
    expect(loadPlan).toHaveBeenCalledWith({
      path: { task_id: "solid-grove.13" }, signal: expect.any(AbortSignal),
    });
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
  });

  it("shows loading while the API response is pending", () => {
    loadPlan.mockReturnValue(new Promise(() => {}));
    renderPlan();
    expect(screen.getByRole("status")).toHaveTextContent("Loading plan…");
  });

  it.each(["API 404: plan not found", "API 503: daemon_unreachable"])(
    "shows API failures: %s", async (message) => {
      loadPlan.mockRejectedValue(new Error(message));
      renderPlan();
      expect(await screen.findByRole("alert")).toHaveTextContent(message);
      expect(loadPlan).toHaveBeenCalledTimes(1);
    },
  );

  it("renders an empty plan successfully", async () => {
    loadPlan.mockResolvedValue({ data: { task_id: "solid-grove.13", content: "" } });
    renderPlan();
    await waitFor(() => expect(screen.queryByRole("status")).not.toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Plan — solid-grove.13" })).toBeInTheDocument();
  });

  it("loads the new task when navigating between plan deep links", async () => {
    loadPlan.mockImplementation(async ({ path }: { path: { task_id: string } }) => ({
      data: { task_id: path.task_id, content: `# Content for ${path.task_id}` },
    }));
    renderPlan();
    expect(await screen.findByRole("heading", { name: "Content for solid-grove.13" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "Next plan" }));
    expect(await screen.findByRole("heading", { name: "Content for solid-grove.14" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Content for solid-grove.13" })).not.toBeInTheDocument();
  });
});
