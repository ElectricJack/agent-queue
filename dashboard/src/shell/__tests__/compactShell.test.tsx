import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import App from "../../App";
import { SHELL_PREFERENCE_DEFAULTS } from "../useShellPreferences";
import { COMPACT_VIEWPORT_QUERY } from "../../hooks/useCompactViewport";
import {
  createFakeDashboardStateServer, TestDashboardState, testQueryClient, type FakeDashboardStateServer,
} from "../../testUtils/dashboardState";

vi.mock("../../panes/registry", () => ({ PANE_REGISTRY: {
  "task-detail": { manifest: { name: "Task", icon: () => null }, Component: () => <p>Task pane body</p> },
} }));
vi.mock("../../api/hooks", () => ({ useProjects: () => ({ data: [{ id: "p1", name: "First" }] }), useAllOpenGates: () => ({ data: [] }) }));
vi.mock("../../api/reviews", () => ({ useWaitingReviewCount: () => 0 }));
vi.mock("../../ws/useEventStream", () => ({ useEventStream: () => {}, useRawEventSubscription: () => {} }));
vi.mock("../../panes/agentPush", () => ({ useAgentPushBridge: () => {} }));
vi.mock("../AgentFlock", () => ({ default: () => <div>Global flock sidebar</div> }));
vi.mock("../ProviderUsageBars", () => ({ default: () => null }));
vi.mock("../ProviderAvailabilityBanner", () => ({ default: () => null }));
vi.mock("../ActivityDrawer", async () => {
  const { useShellPaneStore } = await import("../../panes/store");
  return { default: function Activity() {
    const pane = useShellPaneStore();
    return <><p>Activity body</p><button onClick={() => pane.open("task-detail", { taskId: "t3" })}>Open gate task</button></>;
  } };
});
vi.mock("../palette/Palette", () => ({ Palette: () => null }));
vi.mock("../hotkeys/CheatSheetModal", () => ({ default: () => null }));
vi.mock("../../pages/reviews/ReviewsInbox", async () => {
  const { useShellPaneStore } = await import("../../panes/store");
  return { default: function Inbox() {
    const pane = useShellPaneStore();
    return <><h1>Reviews inbox</h1><button onClick={() => pane.open("task-detail", { taskId: "t2" })}>Open task pane</button></>;
  } };
});
vi.mock("../../pages/project/onboarding", () => ({ default: () => null }));
vi.mock("../../pages/project/onboarding/useProjectRoots", () => ({ useProjectRoots: () => [] }));
vi.mock("../../pages/project/onboarding/useProjectCreatedNavigation", () => ({ useProjectCreatedNavigation: () => () => {} }));

function setCompact(compact: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true, writable: true,
    value: (query: string) => ({
      matches: compact && query === COMPACT_VIEWPORT_QUERY, media: query,
      addEventListener: () => {}, removeEventListener: () => {},
    }),
  });
}

function BackButton() {
  const navigate = useNavigate();
  return <button onClick={() => navigate(-1)}>History back</button>;
}

let server: FakeDashboardStateServer;
beforeEach(() => {
  server = createFakeDashboardStateServer();
  server.write("shell_preferences", {
    ...SHELL_PREFERENCE_DEFAULTS,
    right_surface: { ...SHELL_PREFERENCE_DEFAULTS.right_surface, kind: "pane", width: 760,
      pane: { view: "task-detail", args: { taskId: "t1" } } },
  });
});
afterEach(cleanup);

function renderAt(path: string) {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <TestDashboardState server={server}>
        <MemoryRouter initialEntries={[path]}><App /><BackButton /></MemoryRouter>
      </TestDashboardState>
    </QueryClientProvider>,
  );
}
const puts = () => server.calls.filter((call) => call.op === "put");
const settle = () => act(async () => { await new Promise((done) => setTimeout(done, 50)); });

describe("compact shell (below 768 px)", () => {
  it("hides the rail behind a menu and restores no roaming pane", async () => {
    setCompact(true);
    renderAt("/reviews");
    await screen.findByRole("heading", { name: "Reviews inbox" });
    await settle();
    expect(screen.queryByText("Global flock sidebar")).toBeNull();
    expect(screen.queryByText("Task pane body")).toBeNull();
    expect(screen.getByRole("button", { name: "Open navigation" })).toBeInTheDocument();
  });

  it("the activity sheet fills the screen and writes nothing", async () => {
    setCompact(true);
    renderAt("/reviews");
    fireEvent.click(await screen.findByTitle("Activity drawer (])"));
    expect(await screen.findByRole("dialog", { name: "Activity" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "History back" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Activity" })).toBeNull());
    await settle();
    expect(puts()).toEqual([]);
  });

  it("a pane is a full-screen sheet that Back closes, and writes nothing", async () => {
    setCompact(true);
    renderAt("/reviews");
    fireEvent.click(await screen.findByRole("button", { name: "Open task pane" }));
    const sheet = await screen.findByRole("dialog", { name: "Pane" });
    expect(sheet).toHaveTextContent("Task pane body");
    fireEvent.click(screen.getByRole("button", { name: "History back" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Pane" })).toBeNull());
    expect(screen.getByRole("heading", { name: "Reviews inbox" })).toBeInTheDocument();
    await settle();
    expect(puts()).toEqual([]);
  });

  it("closing the activity sheet leaves its history entry", async () => {
    setCompact(true);
    renderAt("/reviews");
    fireEvent.click(await screen.findByTitle("Activity drawer (])"));
    await screen.findByRole("dialog", { name: "Activity" });
    fireEvent.click(screen.getByRole("button", { name: "Close activity" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Activity" })).toBeNull());
    // The entry it opened is gone: Back does not bring the sheet back.
    fireEvent.click(screen.getByRole("button", { name: "History back" }));
    await settle();
    expect(screen.queryByRole("dialog", { name: "Activity" })).toBeNull();
    expect(puts()).toEqual([]);
  });

  it("a pane opened from the activity sheet replaces it, and Back closes it", async () => {
    setCompact(true);
    renderAt("/reviews");
    fireEvent.click(await screen.findByTitle("Activity drawer (])"));
    fireEvent.click(await screen.findByRole("button", { name: "Open gate task" }));
    expect(await screen.findByRole("dialog", { name: "Pane" })).toHaveTextContent("Task pane body");
    expect(screen.queryByRole("dialog", { name: "Activity" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "History back" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Pane" })).toBeNull());
    await settle();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(puts()).toEqual([]);
  });

  it("desktop keeps the rail column and restores the roaming pane", async () => {
    setCompact(false);
    renderAt("/reviews");
    expect(await screen.findByText("Global flock sidebar")).toBeInTheDocument();
    expect(await screen.findByText("Task pane body")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open navigation" })).toBeNull();
  });
});
