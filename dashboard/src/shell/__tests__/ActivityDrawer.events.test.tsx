import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import ActivityDrawer from "../ActivityDrawer";

const buffer = vi.hoisted(() => ({ events: [] as unknown[] }));

vi.mock("../../api/hooks", () => ({
  useAllOpenGates: () => ({ data: [], isLoading: false }),
  useResolveGate: () => ({ mutate: vi.fn() }),
}));
vi.mock("../../ws/useEventStream", () => ({ useEventStream: () => {} }));
vi.mock("../../ws/EventStreamProvider", () => ({
  useEventBuffer: () => ({ events: buffer.events, clearEvents: vi.fn() }),
}));
vi.mock("../../panes/store", () => ({ useShellPaneStore: () => ({ open: vi.fn() }) }));
vi.mock("../useRightSurface", () => ({
  useRightSurface: () => ({ activityTab: "events", setActivityTab: vi.fn() }),
}));

function Location() {
  const location = useLocation();
  return <output aria-label="Current location">{location.pathname + location.search}</output>;
}

function entry(id: number, event: Record<string, unknown>) {
  return { id, timestamp: new Date(1_000 * id), event };
}

afterEach(() => {
  cleanup();
  buffer.events = [];
});

describe("ActivityDrawer events filtered by request id", () => {
  it("lists only the events one allocation request carried, live or replayed", () => {
    buffer.events = [
      entry(1, { event_type: "pool.lifecycle_changed", profile_id: "standard-high-codex", request_id: "alloc-1" }),
      // A replayed frame nests the bus payload, sometimes as a JSON string.
      entry(2, { event_type: "pool.bounds_changed", payload: JSON.stringify({ request_id: "alloc-1", profile_id: "fast-medium-codex" }) }),
      entry(3, { event_type: "pool.bounds_changed", profile_id: "other", request_id: "alloc-2" }),
      entry(4, { event_type: "task.updated", task_id: "t1" }),
    ];
    render(
      <MemoryRouter initialEntries={["/agents?view=providers&eventRequest=alloc-1"]}>
        <ActivityDrawer />
        <Location />
      </MemoryRouter>,
    );

    const list = screen.getByRole("list", { name: "Events for alloc-1" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("pool.bounds_changed");
    expect(rows[0]).toHaveTextContent("fast-medium-codex");
    expect(rows[1]).toHaveTextContent("pool.lifecycle_changed");
    expect(list).not.toHaveTextContent("alloc-2");

    fireEvent.click(screen.getByRole("button", { name: "Clear request filter" }));
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/agents?view=providers");
    expect(screen.getByLabelText("Current location")).not.toHaveTextContent("eventRequest");
    expect(screen.queryByRole("list", { name: "Events for alloc-1" })).not.toBeInTheDocument();
  });

  it("says so when none of the request's events reached this browser", () => {
    render(
      <MemoryRouter initialEntries={["/agents?eventRequest=alloc-9"]}>
        <ActivityDrawer />
      </MemoryRouter>,
    );
    expect(screen.getByRole("list", { name: "Events for alloc-9" })).toHaveTextContent(/No events for this request/);
  });
});
