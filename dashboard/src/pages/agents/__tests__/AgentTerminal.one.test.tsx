import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import AgentTerminal, { PoolInstanceTerminal } from "../AgentTerminal";
import type { FlockAgent } from "../../../api/agents";

vi.mock("../../../components/InteractiveTerminal", () => ({ default: ({ sessionId }: { sessionId: string }) => <p>Terminal {sessionId}</p> }));
vi.mock("../../../api/agents", () => ({ useStartAgentTerminal: () => ({ mutate: vi.fn(), isPending: false, error: null }) }));
vi.mock("../../../api/hooks", () => ({ useProjects: () => ({ data: [] }) }));

const agent = { id: "worker-a", name: "worker-a", session_id: "s1", session_state: "running", session_provider: "tmux" } as FlockAgent;
const instance = { id: "p1", name: "pool-1", state: "running", provider: "tmux", started_at: 1790000000 };
/** Every width, phone or desktop: an agent terminal is one window. */
const widths = ["(max-width: 767.98px)", "(min-width: 768px)"];
const original = window.matchMedia;

describe("agent terminals", () => {
  it.each(widths)("draw the host shell page's terminal at %s", (query) => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true, writable: true,
      value: (asked: string) => ({ matches: asked === query, media: asked, addEventListener: () => {}, removeEventListener: () => {} }),
    });
    try {
      render(<MemoryRouter><AgentTerminal agent={agent} /></MemoryRouter>);
      expect(screen.getByText("Terminal s1")).toBeInTheDocument();
      render(<MemoryRouter><PoolInstanceTerminal instance={instance} /></MemoryRouter>);
      expect(screen.getByText("Terminal p1")).toBeInTheDocument();
    } finally {
      Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: original });
    }
  });
});
