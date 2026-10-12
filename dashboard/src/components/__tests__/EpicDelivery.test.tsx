import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EpicDeliveryBadge, EpicDeliveryPanel, EpicStatus } from "../EpicDelivery";
import { deliveryCardStatus, describeDelivery, formatAge } from "../epicDeliveryFormat";
import { EPIC_DELIVERY, NOW } from "../../testUtils/epicDelivery";

afterEach(cleanup);

describe("formatAge", () => {
  it("prints the largest whole unit", () => {
    expect(formatAge(45)).toBe("45s");
    expect(formatAge(12 * 60 + 5)).toBe("12m");
    expect(formatAge(3 * 3600 + 59)).toBe("3h");
    expect(formatAge(2 * 86400)).toBe("2d");
    expect(formatAge(-5)).toBe("0s");
  });
});

describe("EpicDeliveryBadge", () => {
  it.each(Object.entries(EPIC_DELIVERY))("%s carries visible text and an icon, not colour alone", (_name, delivery) => {
    const { container } = render(<EpicDeliveryBadge delivery={delivery} now={NOW} />);
    const badge = container.querySelector(`[data-delivery-state="${delivery.state}"]`);
    expect(badge).not.toBeNull();
    expect(badge!.querySelector("svg[aria-hidden]")).not.toBeNull();
    const headline = delivery.state === "implementing" ? delivery.display_status : delivery.label;
    expect(within(badge as HTMLElement).getByText(headline)).toBeInTheDocument();
  });

  it.each(Object.entries(EPIC_DELIVERY))("%s keeps the detail views' dark tone, not the graph's theme tokens", (_name, delivery) => {
    // The graph tokens switch with the graph's light theme while the detail
    // views stay dark, which took this badge to about 3:1 there.
    const { container } = render(<EpicDeliveryBadge delivery={delivery} now={NOW} />);
    const badge = container.querySelector(`[data-delivery-state="${delivery.state}"]`)!;
    expect(badge.className).not.toMatch(/(^|\s)(bg|text)-g-/);
  });

  it("gives every delivery state its own icon", () => {
    const icons = new Map<string, string>();
    for (const delivery of Object.values(EPIC_DELIVERY)) {
      const { container, unmount } = render(<EpicDeliveryBadge delivery={delivery} now={NOW} />);
      icons.set(delivery.state, container.querySelector("svg")!.innerHTML);
      unmount();
    }
    expect(new Set(icons.values()).size).toBe(icons.size);
  });

  it("explains the reason, who acts and how long ago in its tooltip", () => {
    const delivery = EPIC_DELIVERY.strandedReservation;
    render(<EpicDeliveryBadge delivery={delivery} now={NOW} />);
    const title = screen.getByText(delivery.label).closest("[title]")!.getAttribute("title")!;
    expect(title).toContain("Repair stage 13 still holds the branch reservation");
    expect(title).toContain("Responsible: Repair stage 13");
    expect(title).toContain("Last progress 3h ago");
  });

  it("names stale and unavailable evidence", () => {
    expect(describeDelivery(EPIC_DELIVERY.staleEvidence, NOW)).toContain("Evidence is stale");
    expect(describeDelivery(EPIC_DELIVERY.unavailable, NOW)).toContain("Evidence unavailable");
    expect(describeDelivery(EPIC_DELIVERY.delivered, NOW)).not.toContain("Evidence");
  });
});

describe("deliveryCardStatus", () => {
  it("reads a blocked integration hold as blocked, not paused", () => {
    expect(deliveryCardStatus(EPIC_DELIVERY.strandedReservation, "PAUSED")).toBe("BLOCKED");
    expect(deliveryCardStatus(EPIC_DELIVERY.manualPause, "PAUSED")).toBe("PAUSED");
    expect(deliveryCardStatus(EPIC_DELIVERY.delivered, "COMPLETED")).toBe("COMPLETED");
    expect(deliveryCardStatus(null, "IN_PROGRESS")).toBe("IN_PROGRESS");
  });
});

describe("EpicStatus", () => {
  it("never says Paused for an integration hold, and says why the stored status is PAUSED", () => {
    render(<EpicStatus delivery={EPIC_DELIVERY.missingReceipt} status="PAUSED" />);
    expect(screen.getByText("Delivery blocked")).toBeInTheDocument();
    expect(screen.getByText("held by integration")).toHaveAttribute("title", "Stored task status: PAUSED");
    expect(screen.queryByText(/^Paused$/)).not.toBeInTheDocument();
  });

  it("says Paused only for an operator hold", () => {
    render(<EpicStatus delivery={EPIC_DELIVERY.manualPause} status="PAUSED" />);
    expect(screen.getByText("Paused")).toBeInTheDocument();
    expect(screen.queryByText("held by integration")).not.toBeInTheDocument();
  });
});

describe("EpicDeliveryPanel", () => {
  it("shows implementation apart from a blocked delivery, with reason, owner, age and next step", async () => {
    const onOpenTask = vi.fn();
    render(<EpicDeliveryPanel delivery={EPIC_DELIVERY.missingReceipt} onOpenTask={onOpenTask} now={NOW} />);
    const panel = screen.getByTestId("epic-delivery");
    expect(within(panel).getByText("5/5 tasks complete")).toBeInTheDocument();
    expect(within(panel).getByRole("progressbar", { name: "5 of 5 done" })).toBeInTheDocument();
    expect(within(panel).getByText("Integration blocked - final fix not collected")).toBeInTheDocument();
    expect(within(panel).getByText(/completed after the aggregate was frozen/)).toBeInTheDocument();
    expect(within(panel).getByText("Operator")).toBeInTheDocument();
    expect(within(panel).getByText("2h ago")).toBeInTheDocument();
    expect(within(panel).getByText("aq integration reopen-collection calm-grove-25")).toBeInTheDocument();
    await userEvent.click(within(panel).getByRole("button", { name: "Fix K04 knowledge_export API scope" }));
    expect(onOpenTask).toHaveBeenCalledWith("calm-grove-25.5");
    expect(within(panel).getByText("op-2")).toBeInTheDocument();
  });

  it("links the agent holding a stranded reservation", async () => {
    const onOpenTask = vi.fn();
    render(<EpicDeliveryPanel delivery={EPIC_DELIVERY.strandedReservation} onOpenTask={onOpenTask} now={NOW} />);
    expect(screen.getByText("9/9 tasks complete")).toBeInTheDocument();
    expect(screen.getByText("Verification blocked - branch handoff required")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Repair stage 13" }));
    expect(onOpenTask).toHaveBeenCalledWith("repair-op-13");
  });

  it("falls back to router links when no task opener is given", () => {
    render(
      <MemoryRouter>
        <EpicDeliveryPanel delivery={EPIC_DELIVERY.queuedVerifier} now={NOW} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: "Verification task verify-op-1" })).toHaveAttribute("href", "/tasks/verify-op-1");
    expect(screen.getByText("Worker pool")).toBeInTheDocument();
  });

  it("reports stale evidence instead of claiming work is running", () => {
    render(<EpicDeliveryPanel delivery={EPIC_DELIVERY.staleEvidence} now={NOW} />);
    expect(screen.getByText("Verification status stale")).toBeInTheDocument();
    expect(screen.getByText("Evidence is stale")).toBeInTheDocument();
    expect(screen.queryByText("Verifying")).not.toBeInTheDocument();
  });

  it.each([
    ["activeIntegration", "Integrating", "standard-high-claude sess-1"],
    ["approvalHold", "Awaiting approval", "Operator"],
    ["delivered", "Delivered", null],
    ["manualPause", "Paused by operator", "Operator"],
    ["unavailable", "Delivery evidence unavailable", null],
  ] as const)("%s renders its label and responsible party", (name, label, responsible) => {
    render(
      <MemoryRouter>
        <EpicDeliveryPanel delivery={EPIC_DELIVERY[name]} now={NOW} />
      </MemoryRouter>,
    );
    expect(screen.getByText(label)).toBeInTheDocument();
    if (responsible) expect(screen.getByText(responsible)).toBeInTheDocument();
    else expect(screen.queryByText("Responsible")).not.toBeInTheDocument();
  });
});
