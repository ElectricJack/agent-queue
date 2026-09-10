/**
 * The cards' contract, and in particular the one thing they exist to prevent:
 * a frozen reading rendering as a live one.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ProviderUsage from "../ProviderUsage";
import {
  formatAge,
  formatReset,
  seriesLabel,
  sortSnapshots,
  toneFor,
  windowLabel,
} from "../providerUsage";
import type { ProviderUsageResponse, ProviderUsageSnapshot } from "../../../api/hooks";

const api = vi.hoisted(() => ({
  response: null as ProviderUsageResponse | null,
  fail: null as Error | null,
  calls: 0,
}));

vi.mock("../../../api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("../../../api/client");
  return {
    ...actual,
    getProviderUsageApiProvidersUsageGet: async () => {
      api.calls += 1;
      if (api.fail) throw api.fail;
      return { data: api.response };
    },
  };
});

const NOW = 1_789_200_000;

function snap(overrides: Partial<ProviderUsageSnapshot> = {}): ProviderUsageSnapshot {
  return {
    id: 1,
    provider: "claude",
    account_label: "max",
    window: "week",
    scope: "",
    used_percent: 46,
    resets_at: NOW + 3600,
    observed_at: NOW - 60,
    last_seen_at: NOW - 60,
    source: "probe",
    stale: false,
    age_seconds: 60,
    ...overrides,
  } as ProviderUsageSnapshot;
}

function page() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ProviderUsage />
    </QueryClientProvider>,
  );
}

const card = (testid: string) => screen.getByTestId(testid);
const barOf = (testid: string) => within(card(testid)).getByTestId("usage-bar");

beforeEach(() => {
  api.calls = 0;
  api.fail = null;
  api.response = { now: NOW, snapshots: [], series: {} };
});

afterEach(cleanup);

describe("pure formatters", () => {
  it("names a series by provider, window and scope", () => {
    expect(seriesLabel(snap({ provider: "codex", window: "primary" }))).toBe("Codex · primary");
    expect(seriesLabel(snap({ window: "week", scope: "Fable" }))).toBe("Claude · week (Fable)");
    // An unknown provider is printed as the server named it, not dropped.
    expect(seriesLabel(snap({ provider: "gemini", window: "day" }))).toBe("gemini · day");
    expect(windowLabel("")).toBe("limit");
    expect(windowLabel("five_hour")).toBe("five hour");
  });

  it("reports age coarsely and never negatively", () => {
    expect(formatAge(5)).toBe("just now");
    expect(formatAge(600)).toBe("10m ago");
    expect(formatAge(4 * 3600)).toBe("4h ago");
    expect(formatAge(3 * 86_400)).toBe("3d ago");
    // A reading from the future is a clock skew, not a negative age.
    expect(formatAge(-30)).toBe("just now");
  });

  it("formats a reset clock relative to the day, and says so when it has passed", () => {
    const today = new Date(NOW * 1000);
    const at = (d: Date) => Math.floor(d.getTime() / 1000);
    const laterToday = new Date(today);
    laterToday.setHours(today.getHours() + 1, 30, 0, 0);
    expect(formatReset(at(laterToday), NOW)).toMatch(/^resets today \d{2}:\d{2}$/);

    const inThreeDays = new Date(today);
    inThreeDays.setDate(today.getDate() + 3);
    expect(formatReset(at(inThreeDays), NOW)).toMatch(/^resets (Sun|Mon|Tue|Wed|Thu|Fri|Sat) \d{2}:\d{2}$/);

    const inTenDays = new Date(today);
    inTenDays.setDate(today.getDate() + 10);
    expect(formatReset(at(inTenDays), NOW)).toMatch(/^resets \w{3} \d+ \d{2}:\d{2}$/);

    // Already rolled over: never render a past clock as a future one.
    expect(formatReset(NOW - 7200, NOW)).toMatch(/^reset /);

    // A percentage with no clock is still a reading.
    expect(formatReset(null, NOW)).toBeNull();
    expect(formatReset(undefined, NOW)).toBeNull();
  });

  it("colours at 75 and 90, and greys anything stale", () => {
    expect(toneFor(10, false).bar).toBe("bg-indigo-400");
    expect(toneFor(74.9, false).bar).toBe("bg-indigo-400");
    expect(toneFor(75, false).bar).toBe("bg-amber-500");
    expect(toneFor(89.9, false).bar).toBe("bg-amber-500");
    expect(toneFor(90, false).bar).toBe("bg-red-500");
    expect(toneFor(100, false).bar).toBe("bg-red-500");
    // Threshold colour is a claim about now; a stale card makes no such claim.
    expect(toneFor(95, true).bar).toBe("bg-gray-600");
  });

  it("orders cards stably so a ticking percentage cannot reshuffle them", () => {
    const rows = [
      snap({ id: 3, provider: "codex", window: "primary" }),
      snap({ id: 2, provider: "claude", window: "week", scope: "Fable" }),
      snap({ id: 1, provider: "claude", window: "session" }),
    ];
    expect(sortSnapshots(rows).map((r) => r.id)).toEqual([1, 2, 3]);
    // Sorting does not mutate the response array.
    expect(rows.map((r) => r.id)).toEqual([3, 2, 1]);
  });
});

describe("<ProviderUsage />", () => {
  it("renders a fresh card with provider, window, percent, bar and reset time", async () => {
    api.response = {
      now: NOW,
      snapshots: [snap({ id: 7, used_percent: 46, resets_at: NOW + 5400 })],
      series: {},
    };
    page();

    const el = await screen.findByTestId("provider-card-claude-week-");
    expect(el).toHaveAttribute("data-stale", "false");
    expect(within(el).getByText("46%")).toBeInTheDocument();
    expect(within(el).getByText("Claude · week")).toBeInTheDocument();
    expect(within(el).getByText(/^resets /)).toBeInTheDocument();

    const bar = within(el).getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "46");
    expect(barOf("provider-card-claude-week-")).toHaveStyle({ width: "46%" });
  });

  it("marks a stale card and mutes its bar rather than showing a live-looking number", async () => {
    api.response = {
      now: NOW,
      snapshots: [
        snap({
          id: 9,
          provider: "codex",
          window: "primary",
          used_percent: 88,
          stale: true,
          age_seconds: 4 * 3600,
          source: "transcript",
        }),
      ],
      series: {},
    };
    page();

    const el = await screen.findByTestId("provider-card-codex-primary-");
    expect(el).toHaveAttribute("data-stale", "true");
    expect(within(el).getByText("stale · last seen 4h ago")).toBeInTheDocument();

    const bar = within(el).getByTestId("usage-bar");
    // Muted and grey: an unconfirmed 88% must not read as an amber live 88%.
    expect(bar.className).toContain("opacity-40");
    expect(bar.className).toContain("bg-gray-600");
    expect(bar.className).not.toContain("bg-amber-500");
    // The number itself is still shown — the card is honest, not blank.
    expect(within(el).getByText("88%")).toBeInTheDocument();
  });

  it("colours at the 75% and 90% thresholds", async () => {
    api.response = {
      now: NOW,
      snapshots: [
        snap({ id: 1, provider: "claude", window: "session", used_percent: 5 }),
        snap({ id: 2, provider: "claude", window: "week", used_percent: 81 }),
        snap({ id: 3, provider: "codex", window: "primary", used_percent: 94 }),
      ],
      series: {},
    };
    page();

    await screen.findByTestId("provider-card-claude-session-");
    expect(barOf("provider-card-claude-session-").className).toContain("bg-indigo-400");
    expect(barOf("provider-card-claude-week-").className).toContain("bg-amber-500");
    expect(barOf("provider-card-codex-primary-").className).toContain("bg-red-500");
  });

  it("shows that Claude is unavailable beside a Codex reading without fabricating a percentage", async () => {
    api.response = {
      now: NOW,
      snapshots: [snap({ provider: "codex", window: "primary", used_percent: 88 })],
      series: {},
    };
    page();

    expect(await screen.findByTestId("provider-card-codex-primary-")).toBeInTheDocument();
    const unavailable = screen.getByTestId("provider-unavailable-claude");
    expect(within(unavailable).getByText("Usage unavailable")).toBeInTheDocument();
    expect(within(unavailable).getByText("No current Claude usage report is available.")).toBeInTheDocument();
    expect(within(unavailable).queryByRole("progressbar")).toBeNull();
    expect(within(unavailable).queryByText(/%/)).toBeNull();
  });

  it("refreshes provider readings on demand", async () => {
    api.response = {
      now: NOW,
      snapshots: [snap({ id: 12, provider: "claude", used_percent: 46 })],
      series: {},
    };
    page();

    expect(await screen.findByText("46%")).toBeInTheDocument();
    api.response = {
      now: NOW + 60,
      snapshots: [snap({ id: 12, provider: "claude", used_percent: 47 })],
      series: {},
    };
    fireEvent.click(screen.getByRole("button", { name: "Refresh provider usage" }));

    await waitFor(() => expect(screen.getByText("47%")).toBeInTheDocument());
    expect(api.calls).toBeGreaterThanOrEqual(2);
  });

  it("renders an explicit empty state, never a 0% bar", async () => {
    api.response = { now: NOW, snapshots: [], series: {} };
    page();

    expect(await screen.findByText("No provider usage recorded yet")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("surfaces a failed fetch instead of rendering an empty state", async () => {
    api.fail = new Error("boom");
    page();

    await waitFor(() =>
      expect(screen.getByText(/Could not load provider usage: boom/)).toBeInTheDocument(),
    );
    expect(screen.queryByText("No provider usage recorded yet")).toBeNull();
  });
});
