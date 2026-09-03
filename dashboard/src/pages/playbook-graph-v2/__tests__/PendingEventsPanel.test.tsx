import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import PendingEventsPanel, { formatAge } from "../PendingEventsPanel";

describe("PendingEventsPanel", () => {
  it("lists reason and age and offers dispatch and discard", async () => {
    const action = vi.fn();
    const user = userEvent.setup();
    const receivedAt = Date.now() / 1000 - 5400; // 90 minutes ago
    render(<PendingEventsPanel events={[{ pending_event_id: "event-1", event_type: "task.completed", received_at: receivedAt, reason: "stale_contract", attempts: 2 }]} onAction={action} />);

    expect(screen.getByText("stale contract")).toBeInTheDocument();
    const age = screen.getByText("1h old");
    expect(age.tagName).toBe("TIME");
    expect(age).toHaveAttribute("dateTime", new Date(receivedAt * 1000).toISOString());

    await user.click(screen.getByRole("button", { name: "Dispatch event event-1" }));
    await user.click(screen.getByRole("button", { name: "Discard event event-1" }));
    expect(action).toHaveBeenNthCalledWith(1, "dispatch", ["event-1"]);
    expect(action).toHaveBeenNthCalledWith(2, "discard", ["event-1"]);
  });

  it("formats age in the largest whole unit and never goes negative", () => {
    const now = 1_000_000;
    expect(formatAge(now - 5, now)).toBe("5s");
    expect(formatAge(now - 59, now)).toBe("59s");
    expect(formatAge(now - 60, now)).toBe("1m");
    expect(formatAge(now - 3599, now)).toBe("59m");
    expect(formatAge(now - 3600, now)).toBe("1h");
    expect(formatAge(now - 86_399, now)).toBe("23h");
    expect(formatAge(now - 86_400, now)).toBe("1d");
    expect(formatAge(now + 30, now)).toBe("0s"); // clock skew reads as brand new, not as a negative age
  });
});
