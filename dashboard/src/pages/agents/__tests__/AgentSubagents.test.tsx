import { describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach } from "vitest";
import { AgentSubagents, FlockSubagents } from "../AgentMetadata";
import type { FlockAgent } from "../../../api/agents";

afterEach(cleanup);

function agent(overrides: Partial<FlockAgent>): FlockAgent {
  return {
    id: "a", name: "Builder", profile_id: "implementer", role: "worker",
    enabled: true, state: "busy", provider: "anthropic", harness: "claude",
    model: "claude-opus-5", intelligence_class: "standard-high",
    current_task_id: null, current_task_title: null, current_project_id: null,
    session_id: "session-a", session_state: "running", session_provider: "tmux",
    project_id: null, workspace_id: null,
    active_subagent_count: 0 as number | null, subagent_count_complete: true,
    aq_subagent_count: 0, native_subagent_count: 0 as number | null,
    subagents_spawned_total: 0,
    settings: { name: "Builder", profile_id: "implementer", harness: null,
      model: null, intelligence_class: null, enabled: true },
    ...overrides,
  } as FlockAgent;
}

describe("AgentSubagents", () => {
  it("names the native and AQ halves separately rather than one opaque number", () => {
    render(<AgentSubagents agent={agent({
      active_subagent_count: 5, native_subagent_count: 3,
      aq_subagent_count: 2, subagent_count_complete: true,
      subagents_spawned_total: 11,
    })} />);
    expect(screen.getByText("5 sub-agents (3 native · 2 AQ)")).toBeInTheDocument();
    expect(screen.getByTitle(/3 spawned by the harness itself/)).toBeInTheDocument();
    expect(screen.getByTitle(/11 native sub-agents spawned in total/)).toBeInTheDocument();
  });

  it("reports a hooked agent with no children as a real zero, not as unknown", () => {
    render(<AgentSubagents agent={agent({
      active_subagent_count: 0, native_subagent_count: 0,
      aq_subagent_count: 0, subagent_count_complete: true,
    })} />);
    expect(screen.getByText("0 sub-agents (0 native · 0 AQ)")).toBeInTheDocument();
    expect(screen.queryByText(/unknown/i)).not.toBeInTheDocument();
  });

  it("marks the count as a floor when a live session has no harness hooks", () => {
    render(<AgentSubagents agent={agent({
      active_subagent_count: null, native_subagent_count: null,
      aq_subagent_count: 2, subagent_count_complete: false,
    })} />);
    expect(screen.getByText("2+ sub-agents (native unknown · 2 AQ)")).toBeInTheDocument();
    expect(screen.getByTitle(/the total may be higher/)).toBeInTheDocument();
  });
});

describe("FlockSubagents", () => {
  it("shows the flock total with its native and AQ breakdown", () => {
    render(<FlockSubagents rollup={{
      active_total: 7, native_total: 3, aq_total: 4,
      spawned_total: 9, complete: true,
    }} />);
    expect(screen.getByText("7 sub")).toBeInTheDocument();
    expect(screen.getByTitle(/7 active sub-agents across the flock: 3 native, 4 AQ/))
      .toBeInTheDocument();
  });

  it("marks the total as a lower bound when coverage is incomplete", () => {
    render(<FlockSubagents rollup={{
      active_total: 4, native_total: 1, aq_total: 3,
      spawned_total: 1, complete: false,
    }} />);
    expect(screen.getByText("≥4 sub")).toBeInTheDocument();
    expect(screen.getByTitle(/At least 4 active sub-agents/)).toBeInTheDocument();
  });

  it("stays out of the header when a fully covered flock is running nothing", () => {
    const { container } = render(<FlockSubagents rollup={{
      active_total: 0, native_total: 0, aq_total: 0,
      spawned_total: 0, complete: true,
    }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing at all before the first poll returns", () => {
    const { container } = render(<FlockSubagents rollup={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
