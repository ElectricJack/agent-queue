import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import ActivityDrawer from "../ActivityDrawer";
import { askPage } from "../../testUtils/asks";

vi.mock("../../api/asks", () => ({ useAsks: () => ({ data: askPage() }) }));
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

    fireEvent.click(screen.getByText("Architecture proposal"));

    expect(screen.getByLabelText("Current location")).toHaveTextContent("/focus/asks/ask-one");
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
    expect(screen.queryByText("Half-depth chip-scar probe")).not.toBeInTheDocument();
  });
});
