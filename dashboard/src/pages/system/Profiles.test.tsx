import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import SystemProfiles from "./Profiles";

vi.mock("../../api/hooks", () => ({
  useProfiles: () => ({ data: [{
    id: "worker", name: "Worker", default_class: "standard-medium", harness: "codex",
    allowed_tools: [], mcp_servers: [], has_system_prompt: false,
  }], isLoading: false }),
  useIntelligenceClasses: () => ({ data: { classes: [{ id: "standard-medium", mapping: {
    codex: { model: "gpt-5.6-sol", reasoning_effort: "medium" },
  } }] } }),
}));

describe("SystemProfiles", () => {
  it("shows the intelligence class and its resolved launch settings", () => {
    render(<SystemProfiles />);

    expect(screen.getByText("Intelligence class")).toBeInTheDocument();
    expect(screen.getByText("standard-medium")).toBeInTheDocument();
    expect(screen.getByText("gpt-5.6-sol (medium)")).toBeInTheDocument();
    expect(screen.queryByText("Model")).not.toBeInTheDocument();
  });
});
