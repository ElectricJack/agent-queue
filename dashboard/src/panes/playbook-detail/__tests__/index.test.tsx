import type { ReactNode } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { PlaybookSummary } from "../../../api/hooks";
import Pane from "../index";

const mock = vi.hoisted(() => ({ playbook: { id: "audit", scope: "project", scope_identifier: "alpha", triggers: ["timer.24h"], last_run: { run_id: "old-run", status: "completed" } } as PlaybookSummary,
  run: vi.fn(), toggle: vi.fn(), open: vi.fn(), earlierPaused: false, error: null as Error | null }));
vi.mock("../../../api/hooks", () => ({
  usePlaybooks: () => ({ data: [mock.playbook] }),
  usePlaybookRuns: () => ({ data: [{ run_id: "old-run", status: "completed", started_at: 1788200000 }, ...(mock.earlierPaused ? [{ run_id: "earlier-run", status: "paused" }] : [])], error: mock.error }),
  useRunPlaybook: () => ({ mutate: mock.run, isPending: false }),
  useSetPlaybookEnabled: () => ({ mutate: mock.toggle, isPending: false }),
}));
vi.mock("../../../ws/useEventStream", () => ({ useEventStream: vi.fn() }));
vi.mock("../../store", () => ({ useShellPaneStore: () => ({ open: mock.open }) }));
afterEach(cleanup);
beforeEach(() => { mock.run.mockClear(); mock.toggle.mockClear(); mock.open.mockClear(); mock.error = null; mock.earlierPaused = false; mock.playbook.enabled = true; mock.playbook.running_count = 0; });
function show(client = new QueryClient()) {
  const close = vi.fn();
  return { close, ...render(<Pane args={{ playbookId: "audit" }} close={close} setArgs={vi.fn()} setToolbar={vi.fn()} setShortcuts={vi.fn()} />, {
    wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}><MemoryRouter initialEntries={["/projects/alpha/graph"]}>{children}</MemoryRouter></QueryClientProvider>,
  }) };
}
it("shows waiting state, inspectable history, and an editor link back to the graph", () => {
  const { close } = show();
  expect(screen.getByText("Waiting for trigger")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /old-run/ }));
  expect(mock.open).toHaveBeenCalledWith("playbook-run-inspector", { runId: "old-run" });
  const edit = screen.getByRole("link", { name: "Edit definition" });
  expect(edit).toHaveAttribute("href", "/playbooks/audit");
  fireEvent.click(edit); expect(close).toHaveBeenCalledOnce();
});
it("requires an explicit launch and supplies the definition's own project", () => {
  show();
  expect(mock.run).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Run again" }));
  fireEvent.change(screen.getByLabelText("Trigger event (JSON)"), { target: { value: '{"task_id":"t"}' } });
  fireEvent.click(screen.getByRole("button", { name: "Start run" }));
  expect(mock.run).toHaveBeenCalledWith({ playbook_id: "audit", event: { type: "manual", task_id: "t", project_id: "alpha" } }, expect.anything());
});
it.each(['[]', '{bad', '{"project_id":"beta"}'])("does not launch invalid or mismatched event %s", value => {
  show(); fireEvent.click(screen.getByRole("button", { name: "Run again" }));
  fireEvent.change(screen.getByLabelText("Trigger event (JSON)"), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "Start run" }));
  expect(screen.getByRole("alert")).toBeInTheDocument(); expect(mock.run).not.toHaveBeenCalled();
});
it("does not override paused triggers", () => {
  mock.playbook.enabled = false; show();
  expect(screen.getByRole("button", { name: "Run again" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Resume triggers" }));
  expect(mock.toggle).toHaveBeenCalledWith({ playbook_id: "audit", enabled: true });
  expect(mock.run).not.toHaveBeenCalled();
});
it("blocks duplicate launches while a run is active", () => {
  mock.playbook.running_count = 1; show();
  expect(screen.getByRole("button", { name: "Run again" })).toBeDisabled();
});
it("shows run-history failures instead of implying there were no runs", () => {
  mock.error = new Error("offline"); show();
  expect(screen.getByRole("alert")).toHaveTextContent("Could not load runs");
  expect(screen.queryByText("No runs yet.")).not.toBeInTheDocument();
});

it("keeps older human-input runs intact without treating them as a currently executing definition", () => {
  mock.earlierPaused = true; show();
  expect(screen.getByText(/1 earlier runs in this history are paused/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Run again" })).toBeEnabled();
  expect(mock.run).not.toHaveBeenCalled();
});
