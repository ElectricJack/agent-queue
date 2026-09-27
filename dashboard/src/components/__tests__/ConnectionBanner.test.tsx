import { describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { testQueryClient } from "../../testUtils/dashboardState";
import ConnectionBanner from "../ConnectionBanner";

const status = vi.hoisted(() => ({ value: "connecting" as "connecting" | "connected" | "disconnected" }));
vi.mock("../../ws/EventStreamProvider", () => ({ useEventStreamStatus: () => status.value }));

describe("ConnectionBanner", () => {
  it("stays quiet until a live connection is lost, then offers Retry", () => {
    const client = testQueryClient();
    const refetch = vi.spyOn(client, "refetchQueries").mockResolvedValue(undefined);
    // A fresh element per render: React skips re-rendering an identical one.
    const ui = () => (
      <QueryClientProvider client={client}><ConnectionBanner /></QueryClientProvider>
    );
    const { rerender } = render(ui());
    expect(screen.queryByRole("status")).toBeNull(); // first connect is not an outage
    status.value = "connected";
    rerender(ui());
    expect(screen.queryByRole("status")).toBeNull();
    status.value = "disconnected";
    rerender(ui());
    expect(screen.getByRole("status")).toHaveTextContent("Live updates paused");
    act(() => screen.getByRole("button", { name: "Retry" }).click());
    expect(refetch).toHaveBeenCalledWith({ type: "active" });
  });

  it("says Offline when the browser reports it", () => {
    status.value = "connected";
    render(<QueryClientProvider client={testQueryClient()}><ConnectionBanner /></QueryClientProvider>);
    act(() => { window.dispatchEvent(new Event("offline")); });
    expect(screen.getByRole("status")).toHaveTextContent("Offline");
  });
});
