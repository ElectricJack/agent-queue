import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProviderUsageResponse, ProviderUsageSnapshot } from "../../api/hooks";
import type { ProviderAvailabilityStatus, ProviderStatusResponse } from "../../api/providers";
import ProviderUsageBars from "../ProviderUsageBars";

const api = vi.hoisted(() => ({
  usage: null as ProviderUsageResponse | null,
  availability: null as ProviderStatusResponse | null,
  usageCalls: 0,
}));

vi.mock("../../api/client", async (load) => ({
  ...await load<typeof import("../../api/client")>(),
  getProviderUsageApiProvidersUsageGet: async () => {
    api.usageCalls += 1;
    return { data: api.usage };
  },
  getProviderAvailabilityApiProvidersAvailabilityGet: async () => ({ data: api.availability }),
}));

const NOW = 1_789_200_000;

function snap(overrides: Partial<ProviderUsageSnapshot> = {}): ProviderUsageSnapshot {
  return {
    id: 1,
    provider: "claude",
    account_label: "max",
    window: "week",
    scope: "all models",
    used_percent: 46,
    resets_at: NOW + 3 * 86_400,
    observed_at: NOW - 60,
    last_seen_at: NOW - 60,
    source: "probe",
    stale: false,
    age_seconds: 60,
    ...overrides,
  } as ProviderUsageSnapshot;
}

function status(overrides: Partial<ProviderAvailabilityStatus> = {}): ProviderAvailabilityStatus {
  return {
    provider: "claude",
    state: "available",
    half: "launchable",
    reason: "",
    reason_code: "",
    since: NOW - 60,
    until: null,
    held: 0,
    rerouted: 0,
    override: null,
    ...overrides,
  };
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ProviderUsageBars />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  api.usageCalls = 0;
  api.usage = { now: NOW, snapshots: [], series: {} };
  api.availability = { success: true, mode: "enforce", now: NOW, providers: [] };
});

afterEach(cleanup);

describe("<ProviderUsageBars />", () => {
  it("shows exactly one account-wide weekly bar per known provider", async () => {
    api.usage = {
      now: NOW,
      snapshots: [
        snap({ id: 1, provider: "claude", window: "session", scope: "", used_percent: 12 }),
        snap({ id: 2, provider: "claude", window: "week", scope: "all models", used_percent: 61 }),
        snap({ id: 3, provider: "claude", window: "week", scope: "Fable", used_percent: 89 }),
        snap({ id: 4, provider: "codex", window: "secondary", scope: "", used_percent: 17 }),
        snap({ id: 5, provider: "codex", window: "primary", scope: "", used_percent: 73 }),
        snap({ id: 6, provider: "gemini", window: "week", scope: "", used_percent: 99 }),
      ],
      series: {},
    };
    mount();

    const group = await screen.findByTestId("provider-weekly-usage");
    expect(within(group).getAllByRole("progressbar")).toHaveLength(2);
    expect(screen.getByTestId("provider-weekly-claude")).toHaveTextContent("Claude61%");
    expect(screen.getByTestId("provider-weekly-codex")).toHaveTextContent("Codex73%");
    expect(screen.queryByTestId("provider-weekly-gemini")).toBeNull();
    expect(
      within(screen.getByTestId("provider-weekly-codex")).getByTestId("weekly-usage-bar"),
    ).toHaveStyle({ width: "73%" });
    expect(screen.getByTestId("provider-weekly-claude")).toHaveAttribute(
      "title",
      expect.stringMatching(/resets /),
    );
  });

  it("mutes a disabled provider and names its held state and reason", async () => {
    api.usage = { now: NOW, snapshots: [snap({ provider: "codex", window: "primary", scope: "" })], series: {} };
    api.availability = {
      success: true,
      mode: "enforce",
      now: NOW,
      providers: [
        status({
          provider: "codex",
          state: "disabled",
          half: "unavailable",
          reason: "billing pause",
          held: 3,
        }),
      ],
    };
    mount();

    const bar = await screen.findByTestId("provider-weekly-codex");
    expect(bar).toHaveAttribute("data-held", "true");
    expect(bar).toHaveTextContent("Disabled · billing pause · 3 tasks held");
    expect(bar).toHaveAttribute("title", expect.stringContaining("billing pause"));
    expect(within(bar).getByTestId("weekly-usage-bar").className).toContain("bg-gray-600");
  });

  it("shows the server-computed age of a stale reading", async () => {
    api.usage = {
      now: NOW,
      snapshots: [snap({ stale: true, age_seconds: 4 * 3600, used_percent: 88 })],
      series: {},
    };
    mount();

    const bar = await screen.findByTestId("provider-weekly-claude");
    expect(bar).toHaveAttribute("data-stale", "true");
    expect(bar).toHaveTextContent("stale 4h ago");
    expect(bar).toHaveAttribute("title", expect.stringContaining("stale · last seen 4h ago"));
  });

  it("renders nothing for an empty payload", async () => {
    mount();

    await waitFor(() => expect(api.usageCalls).toBe(1));
    expect(screen.queryByTestId("provider-weekly-usage")).toBeNull();
  });
});
