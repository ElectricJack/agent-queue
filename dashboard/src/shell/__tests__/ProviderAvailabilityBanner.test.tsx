/**
 * The outage banner (provider-failover D20): present on desktop while the
 * server says some provider's half is ``unavailable``, and linking to that
 * provider's card on the Metrics tab. Hidden at mobile widths.
 */

import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import ProviderAvailabilityBanner from "../ProviderAvailabilityBanner";
import {
  PROVIDER_AVAILABILITY_KEY,
  type ProviderAvailabilityStatus,
  type ProviderStatusResponse,
} from "../../api/providers";
import { COMPACT_VIEWPORT_QUERY } from "../../hooks/useCompactViewport";

const api = vi.hoisted(() => ({
  response: null as ProviderStatusResponse | null,
  fail: null as Error | null,
  calls: 0,
}));
vi.mock("../../api/client", async (load) => ({
  ...await load<typeof import("../../api/client")>(),
  getProviderAvailabilityApiProvidersAvailabilityGet: async () => {
    api.calls += 1;
    if (api.fail) throw api.fail;
    return { data: api.response };
  },
}));

const NOW = 1_789_200_000;

function status(overrides: Partial<ProviderAvailabilityStatus>): ProviderAvailabilityStatus {
  return {
    provider: "claude", state: "available", half: "launchable", since: NOW - 3600,
    until: null, held: 0, rerouted: 0, ...overrides,
  };
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter><ProviderAvailabilityBanner /></MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, client };
}

function viewport(width: number) {
  const mediaQuery = Object.assign(new EventTarget(), {
    matches: width < 768,
    media: COMPACT_VIEWPORT_QUERY,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
  });
  vi.spyOn(window, "matchMedia").mockImplementation((query) => {
    expect(query).toBe(COMPACT_VIEWPORT_QUERY);
    return mediaQuery;
  });
  return (nextWidth: number) => {
    mediaQuery.matches = nextWidth < 768;
    mediaQuery.dispatchEvent(Object.assign(new Event("change"), {
      matches: mediaQuery.matches, media: mediaQuery.media,
    }));
  };
}

beforeEach(() => {
  viewport(1024);
  api.response = null;
  api.fail = null;
  api.calls = 0;
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("<ProviderAvailabilityBanner />", () => {
  it.each([390, 767.98])("hides unavailable providers at mobile width %s px", async (width) => {
    viewport(width);
    api.response = { now: NOW, providers: [
      status({ provider: "claude", state: "disabled", half: "unavailable" }),
      status({ provider: "codex", state: "exhausted", half: "unavailable" }),
    ] };
    const { client } = mount();
    await waitFor(() => expect(client.getQueryData(PROVIDER_AVAILABILITY_KEY)).toEqual(api.response));
    expect(screen.queryByTestId("provider-availability-banner")).toBeNull();
    expect(screen.queryByRole("link", { name: "View Codex" })).toBeNull();
  });

  it("updates when resizing across the mobile breakpoint, with desktop starting at 768 px", async () => {
    const resize = viewport(768);
    api.response = { now: NOW, providers: [
      status({ provider: "codex", state: "unauthenticated", half: "unavailable" }),
    ] };
    mount();
    const line = await screen.findByTestId("provider-banner-codex");
    const text = line.textContent;

    act(() => resize(767.98));
    expect(screen.queryByTestId("provider-availability-banner")).toBeNull();

    act(() => resize(768));
    expect(screen.getByTestId("provider-banner-codex").textContent).toBe(text);
    expect(screen.getByRole("link", { name: "View Codex" })).toHaveAttribute("href", "/metrics#provider-codex");
  });

  it("stays hidden while every provider is launchable, degraded included", async () => {
    api.response = { now: NOW, providers: [
      status({ provider: "claude" }),
      status({ provider: "codex", state: "degraded", half: "launchable", reason_code: "usage_high" }),
    ] };
    mount();
    await waitFor(() => expect(api.calls).toBe(1));
    expect(screen.queryByTestId("provider-availability-banner")).toBeNull();
  });

  it("names an unavailable provider, why, since when, what moved and what is held", async () => {
    api.response = { now: NOW, providers: [
      status({ provider: "claude" }),
      status({ provider: "codex", state: "unauthenticated", half: "unavailable", since: NOW - 1800,
        rerouted: 5, held: 4 }),
    ] };
    mount();
    const line = await screen.findByTestId("provider-banner-codex");
    expect(line).toHaveTextContent(/^Codex unavailable — logged out since [^·]+ · 5 tasks moved · 4 held View Codex$/);
    expect(screen.queryByTestId("provider-banner-claude")).toBeNull();
    expect(screen.getByRole("link", { name: "View Codex" })).toHaveAttribute("href", "/metrics#provider-codex");
  });

  it("shows one line per unavailable provider, with the expected recovery when known", async () => {
    api.response = { now: NOW, providers: [
      status({ provider: "codex", state: "exhausted", half: "unavailable", until: NOW + 7200 }),
      status({ provider: "claude", state: "disabled", half: "unavailable" }),
    ] };
    mount();
    expect(await screen.findByTestId("provider-banner-codex")).toHaveTextContent(/out of usage since [^·]+ · expected back \S/);
    const claude = screen.getByTestId("provider-banner-claude");
    expect(claude).toHaveTextContent(/^Claude unavailable — disabled by an operator since /);
    // An operator's choice is not an alarm: grey, not red.
    expect(claude.className).toContain("gray");
    expect(screen.getByTestId("provider-banner-codex").className).toContain("red");
  });

  it("calls out an indefinite disable with its reason and author, without expected recovery", async () => {
    api.response = { now: NOW, providers: [
      status({ state: "disabled", half: "unavailable", until: null,
        override: { state: "disabled", until: null, reason: "billing", by: "human:local-operator" } }),
    ] };
    mount();

    const line = await screen.findByTestId("provider-banner-claude");
    expect(line).toHaveTextContent(/disabled indefinitely since [^·]+ · reason: billing · by human:local-operator/);
    expect(line).not.toHaveTextContent(/expected back|in \d/);
  });

  it("shows nothing when the availability read fails", async () => {
    api.fail = new Error("down");
    mount();
    await waitFor(() => expect(api.calls).toBe(1));
    expect(screen.queryByTestId("provider-availability-banner")).toBeNull();
  });
});
