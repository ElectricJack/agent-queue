import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import AgentFlock from "../AgentFlock";
import TopBar from "../TopBar";
import { RightSurfaceProvider, useRightSurface } from "../useRightSurface";
import {
  MAX_PANE_WIDTHS,
  SHELL_PREFERENCE_DEFAULTS,
  useShellPreferences,
  withPaneWidth,
  type ShellPrefs,
} from "../useShellPreferences";
import { ShellPaneProvider } from "../../panes/store";
import { PaletteStateProvider } from "../palette/paletteState";
import {
  createFakeDashboardStateServer,
  TestDashboardState,
  testQueryClient,
  type FakeDashboardStateServer,
} from "../../testUtils/dashboardState";

vi.mock("../../panes/registry", () => ({ PANE_REGISTRY: {} }));
vi.mock("../../api/agents", () => ({
  useAgentFlock: () => ({ data: [], isLoading: false, error: null, refetch: vi.fn() }),
  useFlockSubagents: () => ({ data: undefined }),
  useEditAgent: () => ({ mutate: vi.fn(), isPending: false, variables: undefined, error: null }),
}));
vi.mock("../../api/hooks", () => ({
  usePoolSetEnabled: () => ({ mutate: vi.fn(), isPending: false, variables: undefined, error: null }),
  useAllOpenGates: () => ({ data: [] }),
}));
vi.mock("../../pages/agents/pools", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../pages/agents/pools")>()),
  usePoolFlock: () => ({ entries: [], poolIds: new Set<string>() }),
  useDebouncedBusyPoolEntries: () => ({ busy: [], hiddenCount: 0 }),
}));

let server: FakeDashboardStateServer;

beforeEach(() => {
  server = createFakeDashboardStateServer();
  localStorage.clear();
});

function Status() {
  return <output aria-label="Preference status">{useShellPreferences().status}</output>;
}

function Shell({ client, owner, children }: { client: QueryClient; owner?: string; children: ReactNode }) {
  return (
    <QueryClientProvider client={client}>
      <TestDashboardState server={server} owner={owner}>
        <MemoryRouter>
          <Status />
          {children}
        </MemoryRouter>
      </TestDashboardState>
    </QueryClientProvider>
  );
}

function renderShell(ui: ReactNode, owner?: string) {
  return render(<Shell client={testQueryClient()} owner={owner}>{ui}</Shell>);
}

async function preferencesReady() {
  await waitFor(() => expect(screen.getByLabelText("Preference status")).toHaveTextContent("ready"));
}

function saved(patch: Partial<ShellPrefs>): ShellPrefs {
  return { ...SHELL_PREFERENCE_DEFAULTS, ...patch };
}

function savedSurface(patch: Partial<ShellPrefs["right_surface"]>): ShellPrefs {
  return saved({ right_surface: { ...SHELL_PREFERENCE_DEFAULTS.right_surface, ...patch } });
}

const flockToggle = () => screen.getByRole("button", { name: /^Agent flock/ });

describe("agent-flock collapse", () => {
  it("is saved for the user and follows them to another dashboard, but never to another user", async () => {
    const user = userEvent.setup();
    const laptop = renderShell(<AgentFlock />, "human:ada");
    await preferencesReady();
    expect(flockToggle()).toHaveAttribute("aria-expanded", "true");
    await user.click(flockToggle());
    expect(flockToggle()).toHaveAttribute("aria-expanded", "false");
    await waitFor(() =>
      expect(server.document("shell_preferences", { owner: "human:ada" }).value).toMatchObject({
        agent_flock_collapsed: true,
      }),
    );
    laptop.unmount();

    const desktop = renderShell(<AgentFlock />, "human:ada");
    await preferencesReady();
    expect(flockToggle()).toHaveAttribute("aria-expanded", "false");
    desktop.unmount();

    renderShell(<AgentFlock />, "human:grace");
    await preferencesReady();
    expect(flockToggle()).toHaveAttribute("aria-expanded", "true");
    expect(server.document("shell_preferences", { owner: "human:grace" }).revision).toBe(0);
  });

  it("ignores a legacy browser value and writes nothing to browser storage", async () => {
    localStorage.setItem("aq:flock:collapsed", "true");
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const user = userEvent.setup();
    renderShell(<AgentFlock />);
    await preferencesReady();
    expect(flockToggle()).toHaveAttribute("aria-expanded", "true");
    await user.click(flockToggle());
    await waitFor(() =>
      expect(server.document("shell_preferences").value).toMatchObject({ agent_flock_collapsed: true }),
    );
    expect(setItem).not.toHaveBeenCalled();
    setItem.mockRestore();
  });
});

describe("right surface", () => {
  function surfaceWrapper(client = testQueryClient()) {
    return ({ children }: { children: ReactNode }) => (
      <Shell client={client}>
        <RightSurfaceProvider>{children}</RightSurfaceProvider>
      </Shell>
    );
  }
  const useSurface = () => ({ surface: useRightSurface(), preferences: useShellPreferences() });

  it("shows the default width until the server answers, then the user's width", async () => {
    server.write("shell_preferences", savedSurface({ width: 620 }));
    localStorage.setItem("aq:rightsurface:width", "300");
    const release = server.hold();
    const { result } = renderHook(useSurface, { wrapper: surfaceWrapper() });
    expect(result.current.surface.width).toBe(480);
    release();
    await waitFor(() => expect(result.current.surface.width).toBe(620));
  });

  it("keeps the default width while preferences are unavailable", async () => {
    server.write("shell_preferences", savedSurface({ width: 620 }));
    server.failWith(new Error("API 503: daemon unavailable"));
    const { result } = renderHook(useSurface, { wrapper: surfaceWrapper() });
    await waitFor(() => expect(result.current.preferences.status).toBe("unavailable"));
    expect(result.current.surface.width).toBe(480);
  });

  it("writes a resize once it settles, clamped to the surface's bounds", async () => {
    const { result } = renderHook(useSurface, { wrapper: surfaceWrapper() });
    await waitFor(() => expect(result.current.preferences.status).toBe("ready"));
    act(() => {
      result.current.surface.setWidth(500);
      result.current.surface.setWidth(900);
    });
    expect(result.current.surface.width).toBe(800);
    await waitFor(() =>
      expect(server.document("shell_preferences").value).toMatchObject({ right_surface: { width: 800 } }),
    );
    expect(server.calls.filter((call) => call.op === "put")).toHaveLength(1);
    expect(result.current.surface.width).toBe(800);
  });

  it("reopens the drawer and activity tab the user last left open", async () => {
    server.write("shell_preferences", savedSurface({ kind: "drawer", activity_tab: "events" }));
    const { result } = renderHook(useSurface, { wrapper: surfaceWrapper() });
    await waitFor(() => expect(result.current.surface.kind).toBe("drawer"));
    expect(result.current.surface.activityTab).toBe("events");
  });

  it("leaves restoring a pane to the pane store", async () => {
    server.write("shell_preferences", savedSurface({ kind: "pane", pane: { view: "task-detail", args: {} } }));
    const { result } = renderHook(useSurface, { wrapper: surfaceWrapper() });
    await waitFor(() => expect(result.current.preferences.status).toBe("ready"));
    expect(result.current.surface.kind).toBeNull();
  });

  it("keeps a surface chosen before the server answers, saves it, and saves closing it", async () => {
    server.write("shell_preferences", savedSurface({ kind: "drawer", activity_tab: "events" }));
    const release = server.hold();
    const { result } = renderHook(useSurface, { wrapper: surfaceWrapper() });
    act(() => {
      result.current.surface.setActivityTab("gates");
      result.current.surface.setKind("drawer");
    });
    release();
    await waitFor(() => expect(result.current.preferences.status).toBe("ready"));
    expect(result.current.surface).toMatchObject({ kind: "drawer", activityTab: "gates" });
    await waitFor(() =>
      expect(server.document("shell_preferences").value).toMatchObject({
        right_surface: { kind: "drawer", activity_tab: "gates" },
      }),
    );
    act(() => result.current.surface.setKind(null));
    await waitFor(() =>
      expect(server.document("shell_preferences").value).toMatchObject({ right_surface: { kind: null } }),
    );
  });
});

describe("preference indicator", () => {
  function renderTopBar() {
    return renderShell(
      <ShellPaneProvider>
        <PaletteStateProvider>
          <RightSurfaceProvider>
            <TopBar />
          </RightSurfaceProvider>
        </PaletteStateProvider>
      </ShellPaneProvider>,
    );
  }

  it("says preferences are unavailable while the daemon cannot answer", async () => {
    server.failWith(new Error("API 503: daemon unavailable"));
    renderTopBar();
    expect(await screen.findByText("Preferences unavailable")).toBeInTheDocument();
  });

  it("says a change was not saved when the daemon refuses it, until one lands", async () => {
    const user = userEvent.setup();
    renderTopBar();
    await preferencesReady();
    expect(screen.queryByText(/^Preference/)).not.toBeInTheDocument();
    server.failWith(new Error("API 500: refused"));
    await user.click(screen.getByTitle("Activity drawer (])"));
    expect(await screen.findByText("Preference not saved")).toBeInTheDocument();
    server.failWith(null);
    await user.click(screen.getByTitle("Activity drawer (])"));
    await waitFor(() => expect(screen.queryByText("Preference not saved")).not.toBeInTheDocument());
  });
});

describe("withPaneWidth", () => {
  it("keeps the most recently sized views within the server's bound", () => {
    let prefs: ShellPrefs = SHELL_PREFERENCE_DEFAULTS;
    for (let i = 0; i < MAX_PANE_WIDTHS + 3; i += 1) prefs = withPaneWidth("view-" + i, 300)(prefs);
    prefs = withPaneWidth("view-5", 400)(prefs);
    const views = Object.keys(prefs.pane_widths);
    expect(views).toHaveLength(MAX_PANE_WIDTHS);
    expect(views).not.toContain("view-0");
    expect(views[views.length - 1]).toBe("view-5");
    expect(prefs.pane_widths["view-5"]).toBe(400);
  });
});
