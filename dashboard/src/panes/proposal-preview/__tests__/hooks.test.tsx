import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useProposalGate } from "../hooks";

const useGates = vi.fn();
vi.mock("../../../api/hooks", () => ({
  useGates: (...args: unknown[]) => useGates(...args),
  useResolveGate: vi.fn(),
}));

describe("proposal approval gate", () => {
  it("selects the human gate awaiting this proposal rather than a routing gate", () => {
    const approval = { id: "approval", gate_type: "human", await_id: "prop-current" };
    useGates.mockReturnValue({ data: [
      { id: "routing", gate_type: "routing", subject_id: "prop-current" },
      { id: "other", gate_type: "human", await_id: "prop-other" },
      approval,
    ] });
    const { result } = renderHook(() => useProposalGate("p", "prop-current"));
    expect(result.current.gate).toEqual(approval);
    expect(useGates).toHaveBeenCalledWith({ projectId: "p", status: "open", enabled: true });
  });
});
