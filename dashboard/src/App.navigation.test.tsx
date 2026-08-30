import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import App from "./App";

vi.mock("./panes/registry", () => ({ PANE_REGISTRY: {} }));
vi.mock("./panes/agentPush", () => ({ useAgentPushBridge: () => {} }));
vi.mock("./shell/LeftRail", () => ({ default: () => null }));
vi.mock("./shell/TopBar", () => ({ default: () => null }));
vi.mock("./shell/RightSurface", () => ({ default: () => null }));
vi.mock("./shell/palette/Palette", () => ({ Palette: () => null }));
vi.mock("./shell/hotkeys/CheatSheetModal", () => ({ default: () => null }));
vi.mock("./pages/command-center/Graph", () => ({ default: () => <h1>Command Center graph</h1> }));
vi.mock("./pages/GlobalChat", () => ({ default: () => <h1>Former Home chat</h1> }));
vi.mock("./pages/agents/AgentWorkspace", () => ({ default: () => <h1>Agent flock</h1> }));

function Location() {
  const location = useLocation();
  return <output aria-label="Current location">{location.pathname}{location.search}</output>;
}

function renderApp(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><App /><Location /></MemoryRouter>);
}

afterEach(cleanup);

describe("Dashboard navigation", () => {
  it("keeps only Graph and Tasks in Command Center navigation", async () => {
    renderApp("/command-center/graph");
    await screen.findByRole("heading", { name: "Command Center graph" });
    expect(screen.getByRole("link", { name: "Graph" })).toHaveAttribute("href", "/command-center/graph");
    expect(screen.getByRole("link", { name: "Tasks" })).toHaveAttribute("href", "/command-center/tasks");
    expect(screen.queryByRole("link", { name: "Agents" })).not.toBeInTheDocument();
  });

  it.each(["/agents", "/command-center/agents", "/work/agents", "/work/sessions"])(
    "opens the sidebar Agent flock from %s", async (path) => {
      renderApp(path);
      expect(await screen.findByRole("heading", { name: "Agent flock" })).toBeInTheDocument();
      expect(screen.getByLabelText("Current location").textContent).toBe("/agents");
    },
  );

  it.each(["/", "/old-missing-page"])("lands on Command Center from %s", async (path) => {
    renderApp(path);
    expect(await screen.findByRole("heading", { name: "Command Center graph" })).toBeInTheDocument();
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/command-center/graph");
    expect(screen.queryByRole("heading", { name: "Former Home chat" })).not.toBeInTheDocument();
  });

  it("routes the former Home shortcut to the supervisor's terminal", async () => {
    renderApp("/command-center/graph");
    await screen.findByRole("heading", { name: "Command Center graph" });
    await userEvent.keyboard("g");
    expect(screen.queryByText(/\bhome\b/i)).not.toBeInTheDocument();
    await userEvent.keyboard("h");
    expect(await screen.findByRole("heading", { name: "Agent flock" })).toBeInTheDocument();
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/agents?agent=supervisor-global");
  });

  it("provides a direct Agent flock shortcut", async () => {
    renderApp("/command-center/graph");
    await screen.findByRole("heading", { name: "Command Center graph" });
    await userEvent.keyboard("ga");
    expect(await screen.findByRole("heading", { name: "Agent flock" })).toBeInTheDocument();
    expect(screen.getByLabelText("Current location").textContent).toBe("/agents");
  });
});
