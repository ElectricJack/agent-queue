import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { KnowledgeAdapterError } from "../../../pages/knowledge/adapter";
import { createKnowledgeFixtureAdapter } from "../../../pages/knowledge/fixtureAdapter";
import TaskKnowledgePanel, { type TaskKnowledgePanelProps } from "../TaskKnowledgePanel";

const POSTGRES = "PostgreSQL is the only supported database";
const OUTAGE = "Scheduler outage 2026-09-28: JSON columns without equality";
const POLICY = "Policy: never run the full suite mid-task";

function renderPanel(props: Partial<TaskKnowledgePanelProps> = {}, fixture = createKnowledgeFixtureAdapter()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={client}>
      <TaskKnowledgePanel taskId="swift-grove-52" adapter={fixture} {...props} />
    </QueryClientProvider>,
  );
  return { fixture, ...utils };
}

describe("TaskKnowledgePanel", () => {
  it("announces loading, alerts on failure and says when nothing is cited", async () => {
    const held = createKnowledgeFixtureAdapter();
    const release = held.hold();
    const { unmount } = renderPanel({}, held);
    expect(screen.getByRole("status")).toHaveTextContent("Loading knowledge…");
    release();
    await screen.findByRole("region", { name: "Citations" });
    unmount();

    const failing = createKnowledgeFixtureAdapter();
    failing.failWith(new KnowledgeAdapterError("disabled", "off"));
    const second = renderPanel({}, failing);
    expect(await screen.findByRole("alert")).toHaveTextContent("Knowledge is disabled for this project.");
    second.unmount();

    renderPanel({ taskId: "other-task" });
    expect(await screen.findByText("No knowledge cited yet.")).toBeInTheDocument();
    expect(screen.getByText("No knowledge links.")).toBeInTheDocument();
  });

  it("lists pinned citations with their revision, opening exactly the cited revision", async () => {
    const onOpenRecord = vi.fn();
    const { fixture } = renderPanel({ onOpenRecord });
    const citations = await screen.findByRole("region", { name: "Citations" });
    const items = within(citations).getAllByRole("listitem");
    expect(items).toHaveLength(3);

    const outage = items.find((item) => item.textContent?.includes(OUTAGE))!;
    expect(outage).toHaveTextContent("r2");
    expect(outage).toHaveTextContent("not current — pinned");
    fireEvent.click(within(outage).getByRole("button", { name: "Open revision 2" }));
    expect(onOpenRecord).toHaveBeenCalledWith(fixture.recordIds()[1], "rev-2-2");

    const postgres = items.find((item) => item.textContent?.includes(POSTGRES))!;
    expect(postgres).toHaveTextContent("current");
    expect(postgres).not.toHaveTextContent("not current");

    const unauthorized = items.find((item) => item.getAttribute("data-citation-id") === "cit-3")!;
    expect(unauthorized).toHaveTextContent("Unavailable record");
    expect(unauthorized).toHaveTextContent("not readable");
    expect(within(unauthorized).queryByRole("button")).toBeNull();
    expect(screen.queryByText(POLICY)).toBeNull();
  });

  it("lists informational links and opens readable knowledge endpoints", async () => {
    const onOpenRecord = vi.fn();
    const { fixture } = renderPanel({ onOpenRecord });
    const links = await screen.findByRole("region", { name: "Knowledge links" });
    const items = within(links).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    for (const item of items) expect(item).toHaveAttribute("data-edge-domain", "informational");
    const motivated = items.find((item) => item.getAttribute("data-link-id") === "lnk-t1")!;
    expect(motivated).toHaveTextContent("motivated by");
    fireEvent.click(within(motivated).getByRole("button", { name: "Open" }));
    expect(onOpenRecord).toHaveBeenCalledWith(fixture.recordIds()[0], null);
    const unreadable = items.find((item) => item.getAttribute("data-link-id") === "lnk-t3")!;
    expect(unreadable).toHaveTextContent("Unavailable");
    expect(within(unreadable).queryByRole("button")).toBeNull();
  });

  it("saves a finding only from explicitly selected text", async () => {
    const onSaveFinding = vi.fn();
    const { rerender, fixture } = renderPanel({ onSaveFinding });
    await screen.findByRole("region", { name: "Citations" });
    expect(screen.getByRole("button", { name: "Save finding" })).toBeDisabled();
    expect(screen.getByText("Select text in the task to save a finding.")).toBeInTheDocument();

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    rerender(
      <QueryClientProvider client={client}>
        <TaskKnowledgePanel taskId="swift-grove-52" adapter={fixture} onSaveFinding={onSaveFinding} selectedText="  JSONB everywhere  " />
      </QueryClientProvider>,
    );
    const save = screen.getByRole("button", { name: "Save finding" });
    expect(save).toBeEnabled();
    fireEvent.click(save);
    expect(onSaveFinding).toHaveBeenCalledWith({ taskId: "swift-grove-52", text: "JSONB everywhere" });
  });

  it("carries no task controls", async () => {
    renderPanel({ onSaveFinding: vi.fn(), selectedText: "x" });
    await screen.findByRole("region", { name: "Citations" });
    for (const name of ["Claim", "Complete", "Retry", "Push", "Restart", "Priority"]) {
      expect(screen.queryByRole("button", { name: new RegExp(`^${name}`) })).toBeNull();
    }
  });
});
