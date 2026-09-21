import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import ActivityDrawer from "../ActivityDrawer";

vi.mock("../../api/hooks", () => ({
  useAllOpenGates: () => ({ data: [{
    id: "gate-1",
    gate_type: "review",
    await_id: "review-1",
    status: "open",
    title: "Architecture review",
    project_id: "agent-queue",
  }], isLoading: false }),
  useResolveGate: () => ({ mutate: vi.fn() }),
}));
vi.mock("../../ws/useEventStream", () => ({ useEventStream: () => {} }));
vi.mock("../../panes/store", () => ({ useShellPaneStore: () => ({ open: vi.fn() }) }));
vi.mock("../useRightSurface", () => ({
  useRightSurface: () => ({ activityTab: "gates", setActivityTab: vi.fn() }),
}));

function Location() {
  const location = useLocation();
  return <output aria-label="Current location">{location.pathname}</output>;
}

afterEach(cleanup);

describe("ActivityDrawer review gates", () => {
  it("opens the review and does not expose generic decision buttons", () => {
    render(<MemoryRouter><ActivityDrawer /><Location /></MemoryRouter>);

    fireEvent.click(screen.getByText("Architecture review"));

    expect(screen.getByLabelText("Current location")).toHaveTextContent("/reviews/review-1");
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
  });
});
