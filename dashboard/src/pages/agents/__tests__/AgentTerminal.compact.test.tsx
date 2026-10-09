import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import AgentTerminal, { PoolInstanceTerminal } from "../AgentTerminal";
import type { FlockAgent } from "../../../api/agents";

vi.mock("../../../components/InteractiveTerminal", () => ({ default: ({ sessionId }: { sessionId: string }) => <p>Interactive {sessionId}</p> }));
vi.mock("../../../components/PhoneTerminal", () => ({
  default: ({ sessionId, focusHref }: { sessionId: string; focusHref?: string }) => <p>Phone {sessionId} → {focusHref}</p>,
}));
vi.mock("../../../api/agents", () => ({ useStartAgentTerminal: () => ({ mutate: vi.fn(), isPending: false, error: null }) }));
vi.mock("../../../api/hooks", () => ({ useProjects: () => ({ data: [] }) }));

const agent = { id: "worker-a", name: "worker-a", session_id: "s1", session_state: "running", session_provider: "tmux" } as FlockAgent;
const original = window.matchMedia;
function setCompact(compact: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true, writable: true,
    value: (query: string) => ({ matches: compact && query === "(max-width: 767.98px)", media: query,
      addEventListener: () => {}, removeEventListener: () => {} }),
  });
}
afterEach(() => Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: original }));

describe("agent terminals below 768 px", () => {
  it("open the phone terminal, which attaches at phone size", () => {
    setCompact(true);
    render(<MemoryRouter><AgentTerminal agent={agent} /></MemoryRouter>);
    expect(screen.getByText("Phone s1 → /focus/sessions/s1")).toBeInTheDocument();
    expect(screen.queryByText(/Interactive/)).toBeNull();
  });

  it("pin a pool instance's process in the focus link", () => {
    setCompact(true);
    render(<MemoryRouter><PoolInstanceTerminal instance={{ id: "p1", name: "pool-1", state: "running", provider: "tmux", started_at: 1790000000 }} /></MemoryRouter>);
    expect(screen.getByText("Phone p1 → /focus/sessions/p1?started=1790000000")).toBeInTheDocument();
    expect(screen.queryByText(/Interactive/)).toBeNull();
  });

  it("still attach at desktop widths", () => {
    setCompact(false);
    render(<MemoryRouter><AgentTerminal agent={agent} /></MemoryRouter>);
    expect(screen.getByText("Interactive s1")).toBeInTheDocument();
    render(<MemoryRouter><PoolInstanceTerminal instance={{ id: "p1", name: "pool-1", state: "running", provider: "tmux", started_at: 1790000000 }} /></MemoryRouter>);
    expect(screen.getByText("Interactive p1")).toBeInTheDocument();
    expect(screen.queryByText(/Phone /)).toBeNull();
  });
});
