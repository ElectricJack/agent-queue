import { sdk, resetSDK } from "./liveMocks";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { KnowledgeAdapterError } from "../adapter";
import { createKnowledgeFixtureAdapter, type FixtureOptions } from "../fixtureAdapter";
import Knowledge, { type KnowledgeProps } from "../Knowledge";
import { DEFAULT_KNOWLEDGE_FILTERS } from "../model";

const POSTGRES = "PostgreSQL is the only supported database";
const OUTAGE = "Scheduler outage 2026-09-28: JSON columns without equality";
const RETIRED = "Decision: SQLite retained for local tests";
const POLICY = "Policy: never run the full suite mid-task";

function renderKnowledge(options: FixtureOptions = {}, props: Partial<KnowledgeProps> = {}) {
  const fixture = createKnowledgeFixtureAdapter(options);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={client}>
      <Knowledge adapter={fixture} {...props} />
    </QueryClientProvider>,
  );
  return { fixture, ...utils };
}

const card = (title: string) => screen.getByRole("button", { name: new RegExp(title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")) });
const badge = (scope: HTMLElement, facet: string, value: string) =>
  within(scope).getByText(value, { selector: `[data-badge="${facet}"]` });

describe("Knowledge", () => {
  it("announces loading, then renders the active records", async () => {
    const fixture = createKnowledgeFixtureAdapter();
    const release = fixture.hold();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><Knowledge adapter={fixture} /></QueryClientProvider>);
    expect(screen.getByRole("status")).toHaveTextContent("Loading knowledge…");
    release();
    expect(await screen.findByText(POSTGRES)).toBeInTheDocument();
    expect(screen.queryByRole("status")).toBeNull();
    // The default list is the active lifecycle only.
    expect(screen.queryByText(RETIRED)).toBeNull();
    expect(fixture.calls[0]).toEqual({ op: "list", args: [DEFAULT_KNOWLEDGE_FILTERS, null] });
  });

  it("shows a typed failure as an alert without echoing server text", async () => {
    const fixture = createKnowledgeFixtureAdapter();
    fixture.failWith(new KnowledgeAdapterError("unavailable", "pg down at 10.0.0.1"));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><Knowledge adapter={fixture} /></QueryClientProvider>);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Knowledge is temporarily unavailable.");
    expect(alert).not.toHaveTextContent("10.0.0.1");
  });

  it("explains an empty result and clears the filters that caused it", async () => {
    renderKnowledge();
    await screen.findByText(POSTGRES);
    fireEvent.change(screen.getByLabelText("Search knowledge"), { target: { value: "zzz-nothing" } });
    const empty = (await screen.findByText("No knowledge records match these filters.")).parentElement!;
    // The empty state offers its own reset beside the filter bar's.
    fireEvent.click(within(empty).getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByText(POSTGRES)).toBeInTheDocument();
    expect(screen.getByLabelText("Search knowledge")).toHaveValue("");
  });

  it("shows kind, category, lifecycle and verification as separate text badges", async () => {
    renderKnowledge();
    await screen.findByText(POSTGRES);
    const postgres = card(POSTGRES);
    expect(badge(postgres, "kind", "knowledge")).toHaveTextContent("Kind: knowledge");
    badge(postgres, "category", "fact");
    badge(postgres, "lifecycle", "active");
    badge(postgres, "verification", "verified");
    badge(postgres, "authority", "authoritative");

    badge(card(OUTAGE), "verification", "unverified");
    expect(within(card(OUTAGE)).queryByText("authoritative")).toBeNull();

    const policy = card(POLICY);
    badge(policy, "verification", "disputed");
    expect(badge(policy, "freshness", "stale")).toHaveAttribute("title", "Recheck date has passed");

    fireEvent.change(screen.getByLabelText("Lifecycle"), { target: { value: "" } });
    const retired = await screen.findByRole("button", { name: /SQLite retained/ });
    badge(retired, "lifecycle", "retired");
  });

  it("sends facet filters to the adapter and reports state changes", async () => {
    const onStateChange = vi.fn();
    const { fixture } = renderKnowledge({}, { onStateChange });
    await screen.findByText(POSTGRES);
    fireEvent.change(screen.getByLabelText("Verification"), { target: { value: "disputed" } });
    expect(await screen.findByText(POLICY)).toBeInTheDocument();
    expect(screen.queryByText(POSTGRES)).toBeNull();
    const lists = fixture.calls.filter((call) => call.op === "list");
    const last = lists[lists.length - 1]!;
    expect(last.args[0]).toMatchObject({ verification: "disputed", lifecycle: "active" });
    expect(onStateChange).toHaveBeenLastCalledWith({
      filters: { ...DEFAULT_KNOWLEDGE_FILTERS, verification: "disputed" },
      selection: { recordId: null, revisionId: null },
    });
  });

  it("selects a card into the detail pane and exposes the selection", async () => {
    const onStateChange = vi.fn();
    const { fixture } = renderKnowledge({}, { onStateChange });
    await screen.findByText(POSTGRES);
    expect(screen.getByText("Select a record to read it.")).toBeInTheDocument();
    fireEvent.click(card(OUTAGE));
    expect(await screen.findByRole("heading", { level: 2, name: OUTAGE })).toBeInTheDocument();
    expect(card(OUTAGE)).toHaveAttribute("aria-pressed", "true");
    expect(card(POSTGRES)).toHaveAttribute("aria-pressed", "false");
    expect(onStateChange).toHaveBeenLastCalledWith({
      filters: DEFAULT_KNOWLEDGE_FILTERS,
      selection: { recordId: fixture.recordIds()[1], revisionId: null },
    });
  });

  it("moves between cards with the arrow keys", async () => {
    const { container } = renderKnowledge();
    await screen.findByText(POSTGRES);
    const cards = Array.from(container.querySelectorAll<HTMLElement>("[data-knowledge-row]"));
    expect(cards.length).toBeGreaterThan(2);
    cards[0]!.focus();
    fireEvent.keyDown(cards[0]!, { key: "ArrowDown" });
    expect(document.activeElement).toBe(cards[1]);
    fireEvent.keyDown(cards[1]!, { key: "End" });
    expect(document.activeElement).toBe(cards[cards.length - 1]);
  });

  it("loads further pages on request", async () => {
    renderKnowledge({ pageSize: 2 });
    await screen.findByText(POSTGRES);
    expect(screen.getAllByRole("button", { pressed: false })).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(screen.getAllByRole("button", { pressed: false })).toHaveLength(4));
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Load more" })).toBeNull());
    expect(screen.getAllByRole("button", { pressed: false })).toHaveLength(5);
  });

  it("offers no task controls on a knowledge surface", async () => {
    renderKnowledge({ persona: "supervisor" });
    await screen.findByText(POSTGRES);
    fireEvent.click(card(POSTGRES));
    await screen.findByRole("heading", { level: 2, name: POSTGRES });
    for (const name of ["Claim", "Complete", "Retry", "Push", "Restart", "Priority"]) {
      expect(screen.queryByRole("button", { name: new RegExp(`^${name}`) })).toBeNull();
    }
    expect(screen.getByRole("toolbar", { name: "Knowledge actions" })).toBeInTheDocument();
  });
});

// K10 mounts the delivered presentation over the generated SDK.
import { MemoryRouter, Route, Routes } from "react-router-dom";
import KnowledgeRoute from "../KnowledgeRoute";

describe("live Knowledge route", () => {
  function mount(path = "/projects/p/knowledge") {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}>
      <Routes><Route path="/projects/:projectId/knowledge" element={<KnowledgeRoute />} /></Routes>
    </MemoryRouter></QueryClientProvider>);
  }
  it("keeps the feature unavailable when UI activation is off", async () => {
    resetSDK();
    sdk.recordCapabilities.mockResolvedValue({ data: { capabilities: { enabled: true, ui_enabled: false, enabled_projects: ["p"] } } });
    mount();
    expect(await screen.findByText("Knowledge is unavailable for this project.")).toBeInTheDocument();
    expect(sdk.recordSearch).not.toHaveBeenCalled();
  });
  it("sends URL filters and preserves the search input while typing", async () => {
    resetSDK(); mount("/projects/p/knowledge?verification=disputed&lifecycle=any");
    await screen.findByText("Live finding");
    expect(sdk.recordSearch.mock.calls[0]![0].body).toMatchObject({ project_id: "p", verification: "disputed", lifecycle: null });
    const input = screen.getByLabelText("Search knowledge"); input.focus();
    fireEvent.change(input, { target: { value: "JSONB" } });
    await waitFor(() => expect(sdk.recordSearch).toHaveBeenLastCalledWith(expect.objectContaining({ body: expect.objectContaining({ query: "JSONB" }) })));
    expect(screen.getByLabelText("Search knowledge")).toBe(input);
    expect(input).toHaveFocus();
  });
});
