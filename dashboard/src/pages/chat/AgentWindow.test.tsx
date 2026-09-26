import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import AgentWindow from "../agents/AgentWindow";
import type { FlockAgent } from "../../api/agents";

vi.mock("../GlobalChat", () => ({ default: () => <p>Conversation transcript</p> }));
vi.mock("../agents/AgentTerminal", () => ({ default: () => <p>Terminal input</p> }));
vi.mock("../agents/AgentSettings", () => ({ default: () => <p>Agent settings</p> }));
vi.mock("../agents/AgentMetadata", () => ({ AgentSubagents: () => null, AgentState: () => null, AgentEligibility: () => null }));

function agent(overrides: Partial<FlockAgent> = {}): FlockAgent {
  return {
    id: "supervisor", name: "Agent Q", role: "supervisor", profile_id: "supervisor",
    enabled: true, state: "idle", provider: "openai", harness: "codex", model: "model",
    intelligence_class: "standard-high", current_task_id: null, current_task_title: null,
    current_project_id: null, session_id: "session-one", session_state: "running",
    session_provider: "tmux", project_id: null, workspace_id: null,
    active_subagent_count: 0, subagent_count_complete: true, aq_subagent_count: 0,
    native_subagent_count: 0, subagents_spawned_total: 0,
    settings: { name: "Agent Q", profile_id: "supervisor", harness: null, model: null,
      intelligence_class: null, enabled: true },
    ...overrides,
  };
}
function Location() {
  return <output aria-label="Location">{useLocation().search}</output>;
}
function renderWindow(overrides: Partial<FlockAgent> = {}, url = "/agents?agent=supervisor&keep=1") {
  render(<MemoryRouter initialEntries={[url]}>
    <AgentWindow agent={agent(overrides)} onClose={vi.fn()} resetToken={null} focusRequest={null} />
    <Location />
  </MemoryRouter>);
}

describe("Supervisor conversation entry point", () => {
  it("keeps terminal input as the default and opens the conversation view explicitly", () => {
    renderWindow();
    expect(screen.getByText("Terminal input")).toBeInTheDocument();
    expect(screen.queryByText("Conversation transcript")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Conversations" }));
    expect(screen.getByText("Conversation transcript")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Conversations" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByLabelText("Location")).toHaveTextContent("?agent=supervisor&keep=1&supervisorView=conversations");
    fireEvent.click(screen.getByRole("tab", { name: "Terminal" }));
    expect(screen.getByText("Terminal input")).toBeInTheDocument();
  });

  it.each([{ role: "worker" }, { project_id: "project-one" }] as Partial<FlockAgent>[])("does not offer global conversations on %j", (overrides) => {
    renderWindow(overrides);
    expect(screen.queryByRole("tab", { name: "Conversations" })).not.toBeInTheDocument();
  });

  it.each(["supervisorView=conversations", "conversation=conv-one"])("opens a bookmarked conversation view for %s", (query) => {
    renderWindow({}, `/agents?agent=supervisor&${query}`);
    expect(screen.getByText("Conversation transcript")).toBeInTheDocument();
  });
});
