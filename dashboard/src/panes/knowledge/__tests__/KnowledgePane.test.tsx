import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { createKnowledgeFixtureAdapter, type FixtureOptions, type KnowledgeFixtureAdapter } from "../../../pages/knowledge/fixtureAdapter";
import type { KnowledgeAction, KnowledgeDetailView } from "../../../pages/knowledge/model";
import KnowledgePane from "../KnowledgePane";

const POSTGRES = "PostgreSQL is the only supported database";
const OUTAGE = "Scheduler outage 2026-09-28: JSON columns without equality";
const POLICY = "Policy: never run the full suite mid-task";

interface HarnessProps {
  adapter: KnowledgeFixtureAdapter;
  recordId: string;
  revisionId?: string | null;
  onAction?: (action: KnowledgeAction, detail: KnowledgeDetailView) => void;
  onOpenRecord?: (recordId: string, revisionId: string | null) => void;
  onOpenTask?: (taskId: string) => void;
  onRevision?: (revisionId: string | null) => void;
}

/** Holds the pinned revision the way the route owner would. */
function Harness({ adapter, recordId, revisionId: initial = null, onRevision, ...rest }: HarnessProps) {
  const [revisionId, setRevisionId] = useState<string | null>(initial);
  return (
    <KnowledgePane
      adapter={adapter}
      recordId={recordId}
      revisionId={revisionId}
      onRevisionChange={(next) => { setRevisionId(next); onRevision?.(next); }}
      {...rest}
    />
  );
}

function renderPane(index: number, options: FixtureOptions = {}, props: Omit<HarnessProps, "adapter" | "recordId"> = {}) {
  const fixture = createKnowledgeFixtureAdapter(options);
  const recordId = fixture.recordIds()[index]!;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={client}>
      <Harness adapter={fixture} recordId={recordId} {...props} />
    </QueryClientProvider>,
  );
  return { fixture, recordId, ...utils };
}

const tab = (name: string) => screen.getByRole("tab", { name });
const actionButton = (name: string) => within(screen.getByRole("toolbar", { name: "Knowledge actions" })).queryByRole("button", { name });

describe("KnowledgePane", () => {
  it("renders the header, badges and rendered body with accessible tabs", async () => {
    renderPane(0);
    expect(screen.getByRole("status")).toHaveTextContent("Loading record…");
    expect(await screen.findByRole("heading", { level: 2, name: POSTGRES })).toBeInTheDocument();
    expect(screen.getByText("kn-0a1b2c3d4e5f60718293a4b5c6d7e8f9")).toBeInTheDocument();
    expect(screen.getByText("project agent-queue")).toBeInTheDocument();
    expect(screen.getByText("verified", { selector: "[data-badge=verification]" })).toBeInTheDocument();
    expect(screen.getByText("authoritative", { selector: "[data-badge=authority]" })).toBeInTheDocument();
    expect(screen.getByText(/revision 2 \(current\)/)).toBeInTheDocument();
    expect(screen.getAllByRole("tab")).toHaveLength(4);
    expect(tab("Body")).toHaveAttribute("aria-selected", "true");
    // Markdown body rendered through the shared preview.
    expect(screen.getByRole("heading", { level: 1, name: POSTGRES })).toBeInTheDocument();
  });

  it("renders only server-allowed actions: a worker proposes on a protected record", async () => {
    const onAction = vi.fn();
    renderPane(0, { persona: "worker" }, { onAction });
    await screen.findByRole("heading", { level: 2, name: POSTGRES });
    expect(actionButton("Propose correction")).toBeInTheDocument();
    expect(actionButton("Edit")).toBeNull();
    expect(actionButton("Retire")).toBeNull();
    expect(actionButton("Restore")).toBeNull();
    expect(actionButton("Create task")).toBeInTheDocument();
    fireEvent.click(actionButton("Propose correction")!);
    expect(onAction).toHaveBeenCalledWith("propose_correction", expect.objectContaining({ title: POSTGRES }));
  });

  it("gives a supervisor edit and retire, and a retired record restore with its successor", async () => {
    const onOpenRecord = vi.fn();
    const { fixture, unmount } = renderPane(0, { persona: "supervisor" });
    await screen.findByRole("heading", { level: 2, name: POSTGRES });
    expect(actionButton("Edit")).toBeInTheDocument();
    expect(actionButton("Retire")).toBeInTheDocument();
    expect(actionButton("Propose correction")).toBeNull();
    unmount();

    renderPane(2, { persona: "supervisor" }, { onOpenRecord });
    await screen.findByRole("heading", { level: 2, name: "Decision: SQLite retained for local tests" });
    expect(actionButton("Restore")).toBeInTheDocument();
    expect(actionButton("Retire")).toBeNull();
    const note = screen.getByText(/Retired: Superseded by the Postgres-only test DSN/).closest("[role=note]")!;
    expect(note).toHaveTextContent(`Successor: ${POSTGRES}`);
    fireEvent.click(within(note as HTMLElement).getByRole("button", { name: "Open" }));
    expect(onOpenRecord).toHaveBeenCalledWith(fixture.recordIds()[0], null);
  });

  it("pins a historical revision explicitly and never silently opens current", async () => {
    const onRevision = vi.fn();
    renderPane(1, {}, { onRevision });
    await screen.findByRole("heading", { level: 2, name: OUTAGE });
    expect(screen.getByText("json has no equality operator; use JSONB everywhere.")).toBeInTheDocument();

    fireEvent.click(tab("History"));
    const history = await screen.findByRole("list", { name: "Revision history" });
    const entries = within(history).getAllByRole("listitem");
    expect(entries).toHaveLength(3);
    expect(entries[0]).toHaveTextContent("r3");
    expect(entries[0]).toHaveTextContent("current");
    expect(entries[0]).toHaveAttribute("aria-current", "true");

    fireEvent.click(within(history).getByRole("button", { name: "View revision 2" }));
    expect(onRevision).toHaveBeenCalledWith("rev-2-2");
    const banner = await screen.findByText(/Viewing revision 2 of 3 — not the current revision/);
    expect(screen.getByText(/^revision 2 of 3$/)).toBeInTheDocument();
    // The pinned read is a new query, so the pane re-rendered its list.
    const pinnedHistory = screen.getByRole("list", { name: "Revision history" });
    expect(within(pinnedHistory).getAllByRole("listitem")[1]).toHaveAttribute("aria-current", "true");
    expect(within(pinnedHistory).getAllByRole("listitem")[0]).not.toHaveAttribute("aria-current");

    fireEvent.click(tab("Body"));
    // Revision 2 has no summary yet; the body is that revision's, not the head's.
    expect(screen.queryByText("json has no equality operator; use JSONB everywhere.")).toBeNull();
    expect(screen.getByRole("heading", { level: 2, name: "Fix" })).toBeInTheDocument();
    expect(actionButton("Edit")).toBeDisabled();
    expect(actionButton("Edit")).toHaveAttribute("title", "Edit from the current revision");

    fireEvent.click(within(banner.closest("[role=note]") as HTMLElement).getByRole("button", { name: "View current" }));
    expect(onRevision).toHaveBeenLastCalledWith(null);
    await waitFor(() => expect(screen.queryByText(/not the current revision/)).toBeNull());
    expect(await screen.findByText("json has no equality operator; use JSONB everywhere.")).toBeInTheDocument();
  });

  it("compares a revision with its predecessor on request", async () => {
    renderPane(1);
    await screen.findByRole("heading", { level: 2, name: OUTAGE });
    fireEvent.click(tab("History"));
    await screen.findByRole("list", { name: "Revision history" });
    fireEvent.click(screen.getByRole("button", { name: "Compare revision 2 with previous" }));
    const diff = await screen.findByRole("region", { name: "Changes from revision 1 to revision 2" });
    await waitFor(() => expect(within(diff).getAllByText(/## Fix/, { selector: "[data-diff-op=added]" })).toHaveLength(1));
    expect(screen.getByRole("button", { name: "Compare revision 1 with previous" })).toBeDisabled();
    fireEvent.click(within(diff).getByRole("button", { name: "Close comparison" }));
    expect(screen.queryByRole("region", { name: /Changes from/ })).toBeNull();
  });

  it("shows a redaction tombstone instead of a body, with no sources and no compare", async () => {
    renderPane(4, {}, { revisionId: "rev-5-1" });
    await screen.findByRole("heading", { level: 2, name: "Procedure: restart after an update" });
    expect(screen.getByText("This revision was redacted. Its content is permanently unavailable.")).toHaveAttribute("role", "note");
    expect(document.querySelector(".prose")).toBeNull();
    fireEvent.click(tab("Provenance"));
    expect(screen.getByText("Sources are unavailable for a redacted revision.")).toBeInTheDocument();
    fireEvent.click(tab("History"));
    const history = await screen.findByRole("list", { name: "Revision history" });
    expect(within(history).getAllByRole("listitem")[1]).toHaveTextContent("redacted");
    expect(screen.getByRole("button", { name: "Compare revision 2 with previous" })).toBeDisabled();
  });

  it("reports an unavailable pinned revision and offers current only on request", async () => {
    const onRevision = vi.fn();
    const { fixture } = renderPane(1, {}, { revisionId: "rev-2-99", onRevision });
    expect(await screen.findByRole("alert")).toHaveTextContent("This revision is not available.");
    expect(fixture.calls.filter((call) => call.op === "show")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "View current revision" }));
    expect(onRevision).toHaveBeenCalledWith(null);
    expect(await screen.findByRole("heading", { level: 2, name: OUTAGE })).toBeInTheDocument();
  });

  it("lists sources with evidence labels and links with safe endpoints", async () => {
    const onOpenRecord = vi.fn();
    const onOpenTask = vi.fn();
    const { fixture } = renderPane(1, {}, { onOpenRecord, onOpenTask });
    await screen.findByRole("heading", { level: 2, name: OUTAGE });
    fireEvent.click(tab("Provenance"));

    const sources = screen.getByRole("region", { name: "Sources" });
    expect(within(sources).getByText("task fresh-cascade-88")).toBeInTheDocument();
    expect(within(sources).getAllByText("retained")).toHaveLength(2);
    const url = within(sources).getByText("https://www.postgresql.org/docs/current/datatype-json.html");
    expect(url.closest("a")).toBeNull();
    expect(within(sources).getByText("not retained")).toBeInTheDocument();

    const links = screen.getByRole("region", { name: "Links" });
    const items = within(links).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    for (const item of items) expect(item).toHaveAttribute("data-edge-domain", "informational");

    const pinned = items.find((item) => item.getAttribute("data-link-id") === "lnk-2a")!;
    expect(pinned).toHaveTextContent("pinned to r2");
    fireEvent.click(within(pinned).getByRole("button", { name: "Open revision 2" }));
    expect(onOpenRecord).toHaveBeenCalledWith(fixture.recordIds()[0], "rev-1-2");

    const unauthorized = items.find((item) => item.getAttribute("data-link-id") === "lnk-2b")!;
    expect(unauthorized).toHaveTextContent("Unavailable");
    expect(unauthorized).toHaveTextContent("not readable");
    expect(within(unauthorized).queryByRole("button")).toBeNull();
    expect(screen.queryByText(POLICY)).toBeNull();

    const incoming = items.find((item) => item.getAttribute("data-link-id") === "lnk-2c")!;
    expect(incoming).toHaveTextContent("motivated by");
    fireEvent.click(within(incoming).getByRole("button", { name: "Open task" }));
    expect(onOpenTask).toHaveBeenCalledWith("fresh-cascade-88");
  });

  it("states verification, authority and freshness separately", async () => {
    const { unmount } = renderPane(0);
    await screen.findByRole("heading", { level: 2, name: POSTGRES });
    fireEvent.click(tab("Verification"));
    const authority = screen.getByRole("region", { name: "Authority" });
    expect(within(authority).getByText("authoritative", { selector: "[data-badge=authority]" })).toBeInTheDocument();
    expect(authority).toHaveTextContent("rev-fleet-cascade");
    expect(screen.getByRole("region", { name: "Freshness" })).toHaveTextContent("Fresh");
    unmount();

    renderPane(3);
    await screen.findByRole("heading", { level: 2, name: POLICY });
    fireEvent.click(tab("Verification"));
    expect(within(screen.getByRole("region", { name: "Verification" })).getByText("disputed", { selector: "[data-badge=verification]" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Authority" })).toHaveTextContent("No active authority grant");
    const freshness = screen.getByRole("region", { name: "Freshness" });
    expect(within(freshness).getByText("stale", { selector: "[data-badge=freshness]" })).toBeInTheDocument();
    expect(freshness).toHaveTextContent("Recheck date has passed");
  });

  it("renders markdown through the sanitizing preview: no script, no javascript: href", async () => {
    const fixture = createKnowledgeFixtureAdapter();
    const recordId = fixture.recordIds()[5]!;
    fixture.simulateExternalEdit(recordId, { body: "[run](javascript:alert(1))\n\n<script>alert(1)</script>\n\n[ok](https://example.test)" });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { container } = render(
      <QueryClientProvider client={client}><Harness adapter={fixture} recordId={recordId} /></QueryClientProvider>,
    );
    const ok = await screen.findByRole("link", { name: "ok" });
    expect(ok).toHaveAttribute("href", "https://example.test");
    const run = screen.getByText("run");
    expect(run.closest("a")?.getAttribute("href") ?? "").not.toMatch(/^javascript:/i);
    expect(container.querySelector("script")).toBeNull();
  });

  it("saves an edit against the observed revision and shows the new one", async () => {
    const { fixture } = renderPane(1);
    await screen.findByRole("heading", { level: 2, name: OUTAGE });
    fireEvent.click(actionButton("Edit")!);
    const form = await screen.findByRole("form", { name: /^Edit kn-/ });
    expect(form).toHaveTextContent("Editing from revision 3");
    fireEvent.change(within(form).getByLabelText("Title"), { target: { value: "Outage, revised" } });
    fireEvent.change(within(form).getByLabelText("Why this change"), { target: { value: "Clarify" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("heading", { level: 2, name: "Outage, revised" })).toBeInTheDocument();
    expect(screen.getByText(/revision 4 \(current\)/)).toBeInTheDocument();
    const updates = fixture.calls.filter((call) => call.op === "update");
    expect(updates).toHaveLength(1);
    expect(updates[0]!.args[0]).toMatchObject({ ifRevision: "rev-2-3", draft: { title: "Outage, revised", reason: "Clarify" } });
    expect(screen.getByText("unverified", { selector: "[data-badge=verification]" })).toBeInTheDocument();
  });

  it("reports no-op edits without creating a revision", async () => {
    const { fixture } = renderPane(1);
    await screen.findByRole("heading", { level: 2, name: OUTAGE });
    fireEvent.click(actionButton("Edit")!);
    const form = await screen.findByRole("form", { name: /^Edit kn-/ });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(await within(form).findByRole("status")).toHaveTextContent("No changes to save.");
    expect(fixture.calls.filter((call) => call.op === "update")).toHaveLength(1);
    expect((await fixture.show(fixture.recordIds()[1]!, null)).current.sequence).toBe(3);
  });

  it("on a revision conflict blocks saving until an explicit reload, offers a comparison, and never retries by itself", async () => {
    const { fixture, recordId } = renderPane(1);
    await screen.findByRole("heading", { level: 2, name: OUTAGE });
    fireEvent.click(actionButton("Edit")!);
    const form = await screen.findByRole("form", { name: /^Edit kn-/ });
    fireEvent.change(within(form).getByLabelText("Title"), { target: { value: "Mine" } });

    const moved = fixture.simulateExternalEdit(recordId, { body: "Different body from elsewhere." });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));

    const alert = await within(form).findByRole("alert");
    expect(alert).toHaveTextContent("This record changed while you were editing");
    expect(alert).toHaveTextContent("started from revision 3 and it is now revision 4");
    expect(within(form).getByRole("button", { name: "Save" })).toBeDisabled();
    expect(fixture.calls.filter((call) => call.op === "update")).toHaveLength(1);

    fireEvent.click(within(alert).getByRole("button", { name: "Compare" }));
    const diff = await screen.findByRole("region", { name: "Changes from revision 3 to revision 4" });
    await waitFor(() => expect(within(diff).getByText(/Different body from elsewhere\./, { selector: "[data-diff-op=added]" })).toBeInTheDocument());

    fireEvent.click(within(alert).getByRole("button", { name: "Reload current" }));
    expect(await within(form).findByRole("status")).toHaveTextContent("Rebased onto revision 4. Review your changes, then save again.");
    expect(within(form).queryByRole("alert")).toBeNull();
    expect(within(form).getByLabelText("Title")).toHaveValue("Mine");
    expect(within(form).getByRole("button", { name: "Save" })).toBeEnabled();
    // Reloading rebased the draft; it did not submit it.
    expect(fixture.calls.filter((call) => call.op === "update")).toHaveLength(1);

    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("heading", { level: 2, name: "Mine" })).toBeInTheDocument();
    const updates = fixture.calls.filter((call) => call.op === "update");
    expect(updates).toHaveLength(2);
    expect(updates[1]!.args[0]).toMatchObject({ ifRevision: moved.revisionId });
    expect((updates[0]!.args[0] as { idempotencyKey: string }).idempotencyKey)
      .toBe((updates[1]!.args[0] as { idempotencyKey: string }).idempotencyKey);
    expect(screen.getByText(/revision 5 \(current\)/)).toBeInTheDocument();
  });
});
