/**
 * The task's provider strip (provider-failover D17/D18/D20): the hold in
 * words, "re-routed from … · undo", and the intent chip — every fact taken
 * from the task response, never derived in the browser.
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import TaskProviderRouting, { ProviderIntentChip, type ProviderRoutedTask } from "../TaskProviderRouting";

const api = vi.hoisted(() => ({
  undo: vi.fn(),
  availability: vi.fn(),
}));
vi.mock("../../api/client", async (load) => ({
  ...await load<typeof import("../../api/client")>(),
  postProviderRerouteUndoApiProvidersRerouteUndoPost: api.undo,
  getProviderAvailabilityApiProvidersAvailabilityGet: api.availability,
}));

const NOW = Date.now() / 1000;
let client: QueryClient;
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  api.undo.mockReset();
  api.availability.mockReset();
  api.availability.mockResolvedValue({ data: { success: true, mode: "enforce", now: NOW, providers: [] } });
});
afterEach(() => { cleanup(); client.clear(); });

function mount(task: ProviderRoutedTask) {
  return render(<QueryClientProvider client={client}><TaskProviderRouting task={task} /></QueryClientProvider>);
}

const reroute = {
  id: 7, from_profile_id: "standard-high-codex", to_profile_id: "standard-high-claude",
  from_provider: "codex", to_provider: "claude", reason_code: "provider_unavailable",
  batch_id: "prb-codex-3", actor: "system/playbook:provider-failover", at: NOW - 600,
  undone_at: null, undoable: true,
};

describe("<TaskProviderRouting />", () => {
  it("renders nothing for a task that is neither held nor re-routed", () => {
    const { container } = mount({ id: "t1", provider_intent: "preferred" });
    expect(container).toBeEmptyDOMElement();
  });

  it("renders a hold with its kind in words, the provider's state, recovery and remediation", () => {
    mount({
      id: "t1",
      provider_hold: {
        provider: "codex", vendor: "openai", state: "exhausted", since: NOW - 3600, until: NOW + 7200,
        kind: "awaiting_failover_capacity", ahead: 3, detail: "3 tasks ahead in the trickle",
        profile_id: "standard-high-codex", reason: "weekly limit reached",
        remediation: "wait for the reset",
      },
    });
    const hold = screen.getByTestId("task-provider-hold");
    expect(hold).toHaveAttribute("data-kind", "awaiting_failover_capacity");
    expect(screen.getByTestId("task-provider-hold-kind")).toHaveTextContent("Waiting for failover capacity (3 ahead)");
    expect(hold).toHaveTextContent("Codex");
    expect(hold).toHaveTextContent("Exhausted");
    expect(hold).toHaveTextContent("weekly limit reached");
    expect(screen.getByTestId("task-provider-hold-recovery")).toHaveTextContent(/^expected back in 2h/);
    expect(hold).toHaveTextContent("3 tasks ahead in the trickle");
    expect(screen.getByTestId("task-provider-hold-remediation")).toHaveTextContent("wait for the reset");
  });

  it("says a pinned task waits for its provider, with no recovery time when none is known", () => {
    mount({
      id: "t1",
      provider_hold: {
        provider: "codex", state: "unauthenticated", kind: "provider_pinned", since: NOW - 60, until: null,
      },
    });
    expect(screen.getByTestId("task-provider-hold-kind")).toHaveTextContent("Pinned to this provider");
    expect(screen.getByTestId("task-provider-hold")).toHaveTextContent("Logged out");
    expect(screen.queryByTestId("task-provider-hold-recovery")).toBeNull();
  });

  it("shows where failover moved the task from and undoes that one task", async () => {
    api.undo.mockResolvedValue({ data: { success: true, outcome: "undone", undone: [{ task_id: "t1" }], refused: [] } });
    mount({ id: "t1", rerouted_from: "standard-high-codex", reroute });

    const line = screen.getByTestId("task-reroute");
    expect(line).toHaveTextContent("Re-routed from standard-high-codex to standard-high-claude · undo");
    expect(line).toHaveTextContent("codex → claude · provider unavailable");
    fireEvent.click(screen.getByRole("button", { name: "undo" }));

    await waitFor(() => expect(api.undo).toHaveBeenCalledTimes(1));
    expect(api.undo.mock.calls[0]![0]).toMatchObject({ body: { task_id: ["t1"] } });
    expect(api.undo.mock.calls[0]![0].body).not.toHaveProperty("force");
    expect(screen.queryByRole("alert")).toBeNull();
    // No availability read is needed unless an undo is refused.
    expect(api.availability).not.toHaveBeenCalled();
  });

  it("disables undo when the server says the re-route is not undoable", () => {
    mount({ id: "t1", rerouted_from: "standard-high-codex", reroute: { ...reroute, undoable: false } });
    expect(screen.getByRole("button", { name: "undo" })).toBeDisabled();
  });

  it("shows the refusal and offers Undo anyway only while the original provider is unavailable", async () => {
    api.undo.mockRejectedValueOnce(Object.assign(
      new Error("API 400: t1: provider codex is still unauthenticated; pass force to undo anyway"),
      { payload: { error: "t1: provider codex is still unauthenticated; pass force to undo anyway" } },
    ));
    api.availability.mockResolvedValue({ data: { success: true, mode: "enforce", now: NOW, providers: [
      { provider: "codex", state: "unauthenticated", half: "unavailable" },
      { provider: "claude", state: "available", half: "launchable" },
    ] } });
    mount({ id: "t1", rerouted_from: "standard-high-codex", reroute });

    fireEvent.click(screen.getByRole("button", { name: "undo" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Undo refused: t1: provider codex is still unauthenticated; pass force to undo anyway",
    );
    api.undo.mockResolvedValueOnce({ data: { success: true, outcome: "undone", undone: [{ task_id: "t1" }], refused: [] } });
    fireEvent.click(await screen.findByRole("button", { name: "Undo anyway" }));
    await waitFor(() => expect(api.undo).toHaveBeenCalledTimes(2));
    expect(api.undo.mock.calls[1]![0]).toMatchObject({ body: { task_id: ["t1"], force: true } });
  });

  it("does not offer force for a refusal force cannot fix", async () => {
    api.undo.mockRejectedValueOnce(Object.assign(new Error("API 400: running"), {
      payload: { error: "t1: running or claimed; stop the task before changing its route" },
    }));
    mount({ id: "t1", rerouted_from: "standard-high-codex", reroute });

    fireEvent.click(screen.getByRole("button", { name: "undo" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("running or claimed");
    await waitFor(() => expect(api.availability).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Undo anyway" })).toBeNull();
  });
});

describe("<ProviderIntentChip />", () => {
  it.each([
    ["pinned", "Pinned"],
    ["preferred", "Preferred"],
    ["class_only", "Class only"],
    [null, "Class only"],
  ])("labels %s as %s", (intent, label) => {
    render(<ProviderIntentChip intent={intent} />);
    const chip = screen.getByTestId("provider-intent-chip");
    expect(chip).toHaveTextContent(label);
    expect(chip).toHaveAttribute("title");
  });
});
