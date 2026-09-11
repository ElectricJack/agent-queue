import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import AppShellV2 from "../AppShellV2";
import { useShellPreferences } from "../useShellPreferences";
import { ShellPaneProvider } from "../../panes/store";
import type { PaneEntry } from "../../panes/registry";
import { manifest as stubManifest } from "../../panes/__stub-smoke/manifest";
import {
  createFakeDashboardStateServer,
  TestDashboardState,
  testQueryClient,
} from "../../testUtils/dashboardState";

vi.mock("../../panes/registry", () => ({ PANE_REGISTRY: {} }));
vi.mock("../../panes/agentPush", () => ({ useAgentPushBridge: () => {} }));
vi.mock("../LeftRail", () => ({ default: () => null }));
vi.mock("../TopBar", () => ({ default: () => null }));
vi.mock("../palette/Palette", () => ({ Palette: () => null }));
// The right surface reports what the shell asked it to show.
vi.mock("../RightSurface", async () => {
  const { useRightSurface } = await import("../useRightSurface");
  const { useShellPaneStore } = await import("../../panes/store");
  return {
    default: function SurfaceProbe() {
      const { kind } = useRightSurface();
      const { state } = useShellPaneStore();
      return (
        <>
          <output aria-label="Right surface">{kind ?? "none"}</output>
          <output aria-label="Pane">{state.kind === "open" ? state.view : "closed"}</output>
        </>
      );
    },
  };
});

const REGISTRY: Record<string, PaneEntry> = {
  [stubManifest.id]: { manifest: stubManifest, Component: () => null },
};

function Status() {
  return <output aria-label="Preference status">{useShellPreferences().status}</output>;
}

async function renderShell() {
  render(
    <QueryClientProvider client={testQueryClient()}>
      <TestDashboardState server={createFakeDashboardStateServer()}>
        <MemoryRouter>
          <Status />
          <ShellPaneProvider registryOverride={REGISTRY}>
            <AppShellV2 />
          </ShellPaneProvider>
        </MemoryRouter>
      </TestDashboardState>
    </QueryClientProvider>,
  );
  await waitFor(() =>
    expect(screen.getByLabelText("Preference status")).toHaveTextContent("ready"),
  );
}

const surface = () => screen.getByLabelText("Right surface");
const pane = () => screen.getByLabelText("Pane");

describe("AppShellV2 surface shortcuts", () => {
  it("] opens and closes the activity drawer", async () => {
    const user = userEvent.setup();
    await renderShell();
    expect(surface()).toHaveTextContent("none");

    await user.keyboard("[BracketRight]");
    expect(surface()).toHaveTextContent("drawer");

    await user.keyboard("[BracketRight]");
    expect(surface()).toHaveTextContent("none");
  });

  it("[ opens and closes the pane surface, and ] swaps it for the drawer", async () => {
    const user = userEvent.setup();
    await renderShell();

    await user.keyboard("[BracketLeft]");
    expect(pane()).toHaveTextContent(stubManifest.id);
    await waitFor(() => expect(surface()).toHaveTextContent("pane"));

    await user.keyboard("[BracketLeft]");
    expect(pane()).toHaveTextContent("closed");
    await waitFor(() => expect(surface()).toHaveTextContent("none"));

    await user.keyboard("[BracketLeft]");
    await waitFor(() => expect(surface()).toHaveTextContent("pane"));
    await user.keyboard("[BracketRight]");
    expect(pane()).toHaveTextContent("closed");
    expect(surface()).toHaveTextContent("drawer");
  });
});
