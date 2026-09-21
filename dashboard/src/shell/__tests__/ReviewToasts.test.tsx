import { act, cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ReviewToasts from "../ReviewToasts";
import { __dispatchEventForTests } from "../../ws/useEventStream";
import type { NotifyEvent } from "../../ws/types";

vi.hoisted(() => {
  vi.stubGlobal("WebSocket", class {
    static OPEN = 1;
    static CONNECTING = 0;
    readyState = 0;
    close() {}
  });
});

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
});
afterAll(() => vi.unstubAllGlobals());

describe("ReviewToasts", () => {
  it("opens a submitted review and dismisses it after eight seconds", () => {
    render(<MemoryRouter><ReviewToasts /></MemoryRouter>);

    act(() => __dispatchEventForTests({
      _event_type: "review.submitted",
      event_type: "review.submitted",
      review_id: "review-1",
      title: "Queue architecture",
      kind: "spec",
      revision: 1,
    } as NotifyEvent));

    expect(screen.getByRole("status")).toHaveTextContent("New spec for review: Queue architecture");
    expect(screen.getByRole("link", { name: "Open" })).toHaveAttribute("href", "/reviews/review-1");

    act(() => vi.advanceTimersByTime(8_000));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("describes a revised review with its revision number", () => {
    render(<MemoryRouter><ReviewToasts /></MemoryRouter>);

    act(() => __dispatchEventForTests({
      _event_type: "review.revised",
      event_type: "review.revised",
      review_id: "review-2",
      title: "Implementation plan",
      kind: "plan",
      revision: 3,
    } as NotifyEvent));

    expect(screen.getByRole("status")).toHaveTextContent("Revised: Implementation plan (rev 3)");
  });
});
