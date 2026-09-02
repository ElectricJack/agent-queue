import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import IntelligenceClasses from "../IntelligenceClassesStub";
import type { IntelligenceClassRow } from "../../../api/hooks";

const api = vi.hoisted(() => ({
  listIntelligenceClasses: vi.fn(),
  editIntelligenceClass: vi.fn(),
}));
vi.mock("../../../api/client", () => api);

let rows: IntelligenceClassRow[];
const clients: QueryClient[] = [];

function initialClass(): IntelligenceClassRow {
  return {
    id: "deep-high", name: "Deep high", description: "Complex work", revision: "original-revision",
    mapping: {
      anthropic: { model: "claude-fable-5", thinking: "high" },
      openai: { model: "gpt-5.6-sol", reasoning_effort: "high" },
      codex: { model: "gpt-5.6-sol", reasoning_effort: "high" },
      google: { model: "gemini-custom", thinking_budget: 8192 },
    },
  };
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  clients.push(client);
  client.setQueryData(["intelligence-classes"], { success: true, classes: rows });
  const view = render(
    <QueryClientProvider client={client}><MemoryRouter><IntelligenceClasses /></MemoryRouter></QueryClientProvider>,
  );
  return { ...view, client };
}

function openEditor(name = "Deep high") {
  fireEvent.click(screen.getByRole("button", { name: "Edit " + name }));
  return screen.getByRole("dialog", { name: /edit intelligence class/i });
}

beforeEach(() => {
  vi.clearAllMocks();
  rows = [initialClass()];
  api.listIntelligenceClasses.mockImplementation(async () => ({ data: { success: true, classes: rows } }));
  api.editIntelligenceClass.mockImplementation(async ({ body }: {
    body: { class_id: string; name: string; description: string; mapping: Record<string, unknown> };
  }) => {
    const updated = { id: body.class_id, name: body.name, description: body.description,
      mapping: body.mapping, revision: "saved-revision" };
    rows = rows.map((row) => row.id === updated.id ? updated : row);
    return { data: { success: true, intelligence_class: updated } };
  });
});

afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});

describe("Intelligence class editing", () => {
  it("cancels without writing and reopens the saved values", () => {
    renderPage();
    const dialog = openEditor();
    expect(within(dialog).getByLabelText("Class ID")).toHaveAttribute("readonly");
    fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "Discard me" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.editIntelligenceClass).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Edit Deep high" })).toHaveFocus();
    expect(within(openEditor()).getByLabelText("Name")).toHaveValue("Deep high");
  });

  it("shows custom classes and safely displays null and empty provider slices", () => {
    rows.push({ id: "research-custom", name: "Research", description: "", revision: "custom-revision",
      mapping: { codex: null, google: {}, local: { model: "local-model", vendor_option: true } } });
    renderPage();
    const dialog = openEditor("Research");
    expect(within(dialog).getByLabelText("Class ID")).toHaveValue("research-custom");
    expect(within(dialog).getByLabelText("local model")).toHaveValue("local-model");
    expect(api.editIntelligenceClass).not.toHaveBeenCalled();
  });

  it("saves only edited provider fields, preserving extra options, null and empty slices", async () => {
    rows[0]!.mapping = {
      anthropic: { model: "claude-fable-5", thinking: "future-effort", cache: { ttl: 60 } },
      codex: { model: "gpt-5.6-sol", reasoning_effort: "high", service_tier: "priority" },
      openai: null, google: {},
      local: { model: "local-model", temperature: 0, flags: ["keep"], disabled: null },
      spare: null,
    };
    const { client } = renderPage();
    client.setQueryData(["agents", "flock"], { agents: [{ id: "agent-1", model: "old-model" }], count: 1 });
    client.setQueryData(["agents", "detail", "agent-1"], { model: "old-model" });
    client.setQueryData(["effective-profile", "project-1", "worker"], { model: "old-model" });
    const dialog = openEditor();
    expect(within(dialog).getByLabelText("Claude thinking")).toHaveValue("future-effort");
    fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "My deep tier" } });
    fireEvent.change(within(dialog).getByLabelText("Description"), { target: { value: "Updated description" } });
    fireEvent.change(within(dialog).getByLabelText("Codex model"), { target: { value: "custom-codex-model" } });
    fireEvent.change(within(dialog).getByLabelText("Codex reasoning effort"), { target: { value: "medium" } });
    fireEvent.change(within(dialog).getByLabelText("Google thinking budget"), { target: { value: "0" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.editIntelligenceClass).toHaveBeenCalledWith({
      body: {
        class_id: "deep-high", name: "My deep tier", description: "Updated description",
        expected_revision: "original-revision",
        mapping: {
          anthropic: { model: "claude-fable-5", thinking: "future-effort", cache: { ttl: 60 } },
          codex: { model: "custom-codex-model", reasoning_effort: "medium", service_tier: "priority" },
          openai: null, google: { thinking_budget: 0 },
          local: { model: "local-model", temperature: 0, flags: ["keep"], disabled: null },
          spare: null,
        },
      },
      throwOnError: true,
    });
    expect(await screen.findByRole("button", { name: "Edit My deep tier" })).toBeInTheDocument();
    expect(client.getQueryState(["agents", "flock"])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["agents", "detail", "agent-1"])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["effective-profile", "project-1", "worker"])?.isInvalidated).toBe(true);
    expect(api.listIntelligenceClasses).toHaveBeenCalled();
  });

  it("keeps a dirty form and its original revision when background data changes", async () => {
    const { client } = renderPage();
    const dialog = openEditor();
    fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "My unsaved draft" } });
    rows = [{ ...initialClass(), name: "Another editor", revision: "newer-revision",
      mapping: { anthropic: { model: "someone-elses-model" } } }];
    act(() => client.setQueryData(["intelligence-classes"], { success: true, classes: rows }));
    expect(within(dialog).getByLabelText("Name")).toHaveValue("My unsaved draft");
    expect(within(dialog).getByLabelText("Claude model")).toHaveValue("claude-fable-5");
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.editIntelligenceClass).toHaveBeenCalled());
    expect(api.editIntelligenceClass.mock.calls[0]![0].body).toMatchObject({
      expected_revision: "original-revision", name: "My unsaved draft",
    });
  });

  it.each(["API 409: This class changed; reload before saving.", "API 422: This mapping is not valid."])(
    "retains edits and displays a failed save without retrying: %s", async (message) => {
      api.editIntelligenceClass.mockRejectedValueOnce(new Error(message));
      renderPage();
      const dialog = openEditor();
      fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "Keep this draft" } });
      fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
      expect(await within(dialog).findByRole("alert")).toHaveTextContent(message);
      expect(within(dialog).getByLabelText("Name")).toHaveValue("Keep this draft");
      expect(within(dialog).getByRole("button", { name: "Save" })).toBeEnabled();
      expect(api.editIntelligenceClass).toHaveBeenCalledTimes(1);
    },
  );

  it("blocks duplicate saves and dismissal while saving", async () => {
    let finish!: (value: unknown) => void;
    api.editIntelligenceClass.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    renderPage();
    const dialog = openEditor();
    fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "Pending edit" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: /saving/i })).toBeDisabled());
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(dialog).toBeInTheDocument();
    expect(api.editIntelligenceClass).toHaveBeenCalledTimes(1);
    await act(async () => finish({ data: { success: true, intelligence_class: { ...rows[0], name: "Pending edit", revision: "saved" } } }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("validates empty names and negative budgets before sending a request", () => {
    renderPage();
    const dialog = openEditor();
    fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "  " } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent(/name/i);
    fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "Deep high" } });
    fireEvent.change(within(dialog).getByLabelText("Google thinking budget"), { target: { value: "-1" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent(/budget/i);
    expect(api.editIntelligenceClass).not.toHaveBeenCalled();
  });

  it("validates advanced JSON and saves custom provider options without rebuilding them", async () => {
    renderPage();
    const dialog = openEditor();
    fireEvent.click(within(dialog).getByRole("button", { name: /advanced mapping json/i }));
    const json = within(dialog).getByLabelText("Provider mapping JSON");
    for (const invalid of ["{", "[]", '{"custom":{"budget":1e999}}']) {
      fireEvent.change(json, { target: { value: invalid } });
      fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
      expect(within(dialog).getByRole("alert")).toBeInTheDocument();
      expect(api.editIntelligenceClass).not.toHaveBeenCalled();
    }
    fireEvent.change(json, { target: { value: '{"codex":null,"google":{},"local":{"model":"local-model","custom":{"nested":[false,null,0]}}}' } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.editIntelligenceClass.mock.calls[0]![0].body.mapping).toEqual({
      codex: null, google: {}, local: { model: "local-model", custom: { nested: [false, null, 0] } },
    });
  });

  it("keeps saved Fable reasoning and prevents a newly introduced off combination", () => {
    rows = [{ ...initialClass(), id: "deep-off", name: "Deep off",
      mapping: { anthropic: { model: "claude-fable-5", thinking: "low" } } }];
    renderPage();
    const dialog = openEditor("Deep off");
    const thinking = within(dialog).getByLabelText("Claude thinking");
    expect(thinking).toHaveValue("low");
    expect(within(thinking).queryByRole("option", { name: "off" })).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: /advanced mapping json/i }));
    fireEvent.change(within(dialog).getByLabelText("Provider mapping JSON"), {
      target: { value: '{"anthropic":{"model":"claude-fable-5","thinking":"off"}}' },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent(/Fable.*off/i);
    expect(api.editIntelligenceClass).not.toHaveBeenCalled();
  });

  it("reloads a conflicted revision for reopening while preserving the open draft", async () => {
    renderPage();
    const dialog = openEditor();
    fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "My draft" } });
    rows = [{ ...initialClass(), name: "Saved elsewhere", revision: "other-revision" }];
    api.editIntelligenceClass.mockRejectedValueOnce(new Error("API 409: Revision conflict."));
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/409/);
    await screen.findByRole("button", { name: "Edit Saved elsewhere" });
    expect(within(dialog).getByLabelText("Name")).toHaveValue("My draft");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    const reopened = openEditor("Saved elsewhere");
    expect(within(reopened).getByLabelText("Name")).toHaveValue("Saved elsewhere");
    fireEvent.change(within(reopened).getByLabelText("Name"), { target: { value: "Reviewed new version" } });
    fireEvent.click(within(reopened).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.editIntelligenceClass.mock.calls[1]![0].body.expected_revision).toBe("other-revision");
  });

  it("keeps keyboard focus inside the dialog and Escape cancels without writing", () => {
    renderPage();
    const dialog = openEditor();
    expect(within(dialog).getByLabelText("Name")).toHaveFocus();
    const close = within(dialog).getByRole("button", { name: "Close editor" });
    const save = within(dialog).getByRole("button", { name: "Save" });
    close.focus();
    fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
    expect(save).toHaveFocus();
    fireEvent.keyDown(save, { key: "Tab" });
    expect(close).toHaveFocus();
    fireEvent.keyDown(close, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.editIntelligenceClass).not.toHaveBeenCalled();
  });

  it("preserves an empty mapping on metadata-only edits", async () => {
    rows = [{ ...initialClass(), mapping: {} }];
    renderPage();
    const dialog = openEditor();
    fireEvent.change(within(dialog).getByLabelText("Description"), { target: { value: "Still no overrides" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.editIntelligenceClass.mock.calls[0]![0].body.mapping).toEqual({});
  });


  it("rejects newly entered surrounding model whitespace without sending it", () => {
    renderPage();
    const dialog = openEditor();
    fireEvent.change(within(dialog).getByLabelText("Codex model"), { target: { value: " custom-model " } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent(/model.*whitespace/i);
    expect(api.editIntelligenceClass).not.toHaveBeenCalled();
  });

  it("clears individual overrides without dropping reasoning or custom options", async () => {
    rows[0]!.mapping = {
      anthropic: { model: "claude-fable-5", thinking: "high", cache: { ttl: 60 } },
      codex: { model: "gpt-5.6-sol", reasoning_effort: "high", service_tier: "priority" },
    };
    renderPage();
    const dialog = openEditor();
    fireEvent.change(within(dialog).getByLabelText("Claude model"), { target: { value: "" } });
    fireEvent.change(within(dialog).getByLabelText("Codex reasoning effort"), { target: { value: "" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.editIntelligenceClass.mock.calls[0]![0].body.mapping).toEqual({
      anthropic: { thinking: "high", cache: { ttl: 60 } },
      codex: { model: "gpt-5.6-sol", service_tier: "priority" },
    });
  });

});
