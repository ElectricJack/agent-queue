/** Integration coverage for the seam the unit tests mock away: a real
 *  <ReactFlow> rendering real node cards inside the real view. */
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import PlaybookGraphView from "../PlaybookGraphView";
import { REVIEW_PROMPT, graph, layout } from "./fixtures";

const query = vi.hoisted(() => ({ state: {} as Record<string, unknown>, refetch: vi.fn() }));
vi.mock("../../../api/hooks", () => ({
  usePlaybookGraph: (playbookId?: string) => {
    query.state.playbookId = playbookId;
    return { ...query.state, refetch: query.refetch };
  },
}));

beforeEach(() => {
  query.refetch.mockReset();
  query.state = {
    data: { success: true, playbook: { id: "review-flow" }, graph, layout, legend: {} },
    isPending: false,
    isError: false,
    error: null,
  };
});
afterEach(cleanup);

describe("playbook graph selection (real React Flow)", () => {
  it("selects the clicked node and populates the inspector", async () => {
    const user = userEvent.setup();
    render(<PlaybookGraphView playbookId="review-flow" />);

    const card = await screen.findByRole("button", { name: "Inspect node review" });
    await user.click(card);

    const inspector = screen.getByRole("complementary", { name: "Node inspector" });
    expect(within(inspector).getByRole("heading", { name: "review" })).toBeInTheDocument();
    expect(within(inspector).getByText(REVIEW_PROMPT, { collapseWhitespace: false })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Inspect node review" })).toHaveAttribute("aria-pressed", "true");
  });

  it("swaps the selection when a second node is clicked", async () => {
    const user = userEvent.setup();
    render(<PlaybookGraphView playbookId="review-flow" />);

    await user.click(await screen.findByRole("button", { name: "Inspect node review" }));
    await user.click(screen.getByRole("button", { name: "Inspect node approve" }));

    const inspector = screen.getByRole("complementary", { name: "Node inspector" });
    expect(within(inspector).getByRole("heading", { name: "approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Inspect node approve" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Inspect node review" })).toHaveAttribute("aria-pressed", "false");
  });

  it("clears the selection on a pane click and keeps the laid-out camera", async () => {
    const user = userEvent.setup();
    const { container } = render(<PlaybookGraphView playbookId="review-flow" />);

    const nodeTransform = () =>
      (container.querySelector('.react-flow__node[data-id="review"]') as HTMLElement).style.transform;
    const cameraTransform = () =>
      (container.querySelector(".react-flow__viewport") as HTMLElement).style.transform;
    const nodeBefore = nodeTransform();
    const cameraBefore = cameraTransform();

    await user.click(await screen.findByRole("button", { name: "Inspect node review" }));
    expect(nodeTransform()).toBe(nodeBefore);
    expect(cameraTransform()).toBe(cameraBefore);

    fireEvent.click(container.querySelector(".react-flow__pane")!);
    expect(screen.getByText("Select a node to inspect its compiled configuration.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Inspect node review" })).toHaveAttribute("aria-pressed", "false");
    expect(query.refetch).not.toHaveBeenCalled();
  });

  it("clears the selection on Escape", async () => {
    const user = userEvent.setup();
    render(<PlaybookGraphView playbookId="review-flow" />);

    await user.click(await screen.findByRole("button", { name: "Inspect node review" }));
    screen.getByRole("region", { name: "Playbook graph" }).focus();
    await user.keyboard("{Escape}");

    expect(screen.getByText("Select a node to inspect its compiled configuration.")).toBeInTheDocument();
  });

  it("selects via keyboard activation of the focused card", async () => {
    const user = userEvent.setup();
    render(<PlaybookGraphView playbookId="review-flow" />);

    (await screen.findByRole("button", { name: "Inspect node approve" })).focus();
    await user.keyboard("{Enter}");

    const inspector = screen.getByRole("complementary", { name: "Node inspector" });
    expect(within(inspector).getByRole("heading", { name: "approve" })).toBeInTheDocument();
  });

  it("drops the selection when the page switches to a different playbook", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<PlaybookGraphView playbookId="review-flow" />);

    await user.click(await screen.findByRole("button", { name: "Inspect node review" }));
    rerender(<PlaybookGraphView playbookId="other-flow" />);

    expect(screen.getByText("Select a node to inspect its compiled configuration.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Inspect node review" })).toHaveAttribute("aria-pressed", "false");
  });
});
