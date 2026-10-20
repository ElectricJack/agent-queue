import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import TaskWaitPanel from "../TaskWaitPanel";
import WaitingOn from "../../pages/WaitingOn";
import StatusBadge from "../StatusBadge";
import { waitExplain, waitGraph } from "../../api/client";

vi.mock("../../api/client", () => ({ waitExplain: vi.fn(), waitGraph: vi.fn() }));
vi.mock("../../api/hooks", () => ({ useProjects: () => ({ data: [{ id: "p", name: "Project P" }] }) }));

const record = {
  wait_id: "w", project_id: "p", owner: { kind: "scheduler", id: "repair" },
  lifecycle: "READY", age_seconds: 1260, age_unknown: false, outcome: "pending",
  verified: {
    kind: "resource", condition: { resource_key: "pool" }, state: "verified", evidence: "resource:pool:r91",
    source_key: "pool", source_generation: 1, source_revision: "91", observed_at: 1800000000,
    freshness_seconds: 2, facts: { used: 2, capacity: 2 },
  },
  claimed: "CI passed", sleep: { decision: "in_place", reason: "Pool capacity full", inputs: { threshold: 120 } },
  deadlines: [{ type: "escalation", due_at: 1800001800 }],
  effective_priority: { base: 100, value: 20, donor: "task:delivery", donor_path: ["task:delivery", "task:repair"] },
  chain: [{ from: "task:repair", to: "resource:pool", relation: "capacity" }],
  chain_truncated: true, reservations: [{ state: "pending", rank: 1 }],
  continuation: { resume_state: "sleeping", held_messages: 3 }, policy_layer: "project",
};
const graph = {
  projects: [{ project_id: "p", waits: [record], total_waits: 2, counts: [{ kind: "resource", count: 2, decision: "in_place" }],
    truncated: { waits: true }, longest_chains: [], branch_blockers: [{ id: "repair", reservations: [{ state: "pending" }] }],
    escalated_waits: [{ id: "escalation-1" }], held_message_backlog: [{ held_messages: 3 }], deadlocks: [] }],
  total_projects: 2, truncated: true,
};

function mount(ui: React.ReactNode, route = "/waiting") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter></QueryClientProvider>);
  return client;
}

afterEach(cleanup);
beforeEach(() => vi.resetAllMocks());

describe("wait explanation surfaces", () => {
  it("shows the scheduler blocker for a READY unclaimed task, with separate claimed commentary", async () => {
    vi.mocked(waitExplain).mockResolvedValue({ data: { waits: [record], task: { id: "repair", lifecycle: "READY", base_priority: 100, effective_priority: 20 }, total: 2, truncated: true } } as never);
    mount(<TaskWaitPanel taskId="repair" projectId="p" />);
    const panel = screen.getByRole("region", { name: "Wait explanation" });
    await within(panel).findByText("resource");
    expect(waitExplain).toHaveBeenCalledWith(expect.objectContaining({ body: { task_id: "repair", project_id: "p" }, throwOnError: true }));
    expect(panel.textContent!.indexOf("Verified evidence")).toBeLessThan(panel.textContent!.indexOf("Claimed commentary"));
    expect(within(panel).getByText("CI passed")).toBeInTheDocument();
    expect(within(panel).getByText(/Age: 21m/)).toBeInTheDocument();
    expect(within(panel).getByText(/Pool capacity full/)).toBeInTheDocument();
    expect(within(panel).getByText(/base 100, effective 20/)).toBeInTheDocument();
    expect(within(panel).getByText(/showing 1 of 2/)).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "Project wait graph" })).toHaveAttribute("href", "/waiting?project=p");
  });

  it("does not invent an age or imply unavailable evidence is verified", async () => {
    vi.mocked(waitExplain).mockResolvedValue({ data: { waits: [{ ...record, age_unknown: true, verified: { ...record.verified, state: "stale" } }] } } as never);
    mount(<TaskWaitPanel taskId="repair" projectId="p" />);
    expect(await screen.findByText(/Age: age_unknown/)).toBeInTheDocument();
    expect(screen.getByText("stale")).toBeInTheDocument();
  });

  it("shows empty state and query failure distinctly", async () => {
    vi.mocked(waitExplain).mockResolvedValueOnce({ data: { waits: [] } } as never);
    const client = mount(<TaskWaitPanel taskId="repair" projectId="p" />);
    expect(await screen.findByText("No active wait records.")).toBeInTheDocument();
    vi.mocked(waitExplain).mockRejectedValueOnce(new Error("wait.unavailable"));
    await client.invalidateQueries({ queryKey: ["wait-explain"] });
    expect(await screen.findByRole("alert")).toHaveTextContent("wait.unavailable");
  });

  it("renders fleet columns, truncation and admission diagnostics, and selects project scope", async () => {
    vi.mocked(waitGraph).mockResolvedValue({ data: { graph } } as never);
    mount(<WaitingOn />);
    expect(await screen.findByText("resource")).toBeInTheDocument();
    expect(waitGraph).toHaveBeenCalledWith(expect.objectContaining({ body: { fleet: true } }));
    expect(screen.getByRole("columnheader", { name: "Verified evidence" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Claimed commentary" })).toBeInTheDocument();
    const cells = screen.getAllByRole("cell");
    expect(cells[1]).not.toHaveTextContent("CI passed");
    expect(cells[2]).toHaveTextContent("CI passed");
    expect(screen.getByRole("link", { name: "repair" })).toHaveAttribute("href", "/tasks/repair");
    expect(screen.getByText("Project list truncated.")).toBeInTheDocument();
    expect(screen.getByText(/Project results truncated: waits/)).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "p" } });
    await waitFor(() => expect(waitGraph).toHaveBeenCalledWith(expect.objectContaining({ body: { project_id: "p" } })));
  });

  it("handles direct graph projections and capability denials", async () => {
    vi.mocked(waitGraph).mockResolvedValueOnce({ data: graph } as never);
    const client = mount(<WaitingOn />, "/waiting?project=p");
    expect(await screen.findByText("resource")).toBeInTheDocument();
    vi.mocked(waitGraph).mockRejectedValueOnce(new Error("capability denied: waits.fleet_summary"));
    await client.invalidateQueries({ queryKey: ["wait-graph"] });
    expect(await screen.findByRole("alert")).toHaveTextContent("capability denied");
  });

  it.each(["WAITING", "HIBERNATED", "RESUME_PENDING"])("renders the %s lifecycle pill", (status) => {
    mount(<StatusBadge status={status} />);
    expect(screen.getByText(status.replace(/_/g, " "))).toBeInTheDocument();
  });
});
