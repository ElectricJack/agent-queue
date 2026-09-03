import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import DiagnosticsBanner from "../DiagnosticsBanner";
import { graph } from "./fixtures";

afterEach(cleanup);

/** One diagnostic of every severity. The error is the projection's own — the
 *  stub registry knows none of the artifact's commands — and the other three
 *  are severities `project_graph` does not raise for this artifact but the
 *  banner has to rank and render. */
const ALL = [
  graph.diagnostics![0]!,
  {
    severity: "warning" as const,
    code: "stale_contract",
    message: "gate_create's contract changed since this artifact was compiled",
    rule_id: "sweep-on-spec-approved",
    step_id: "open-gate",
    source: null,
  },
  { severity: "question" as const, code: "compile_question", message: "Unresolved question on check-gate", rule_id: null, step_id: "check-gate", source: null },
  {
    severity: "info" as const,
    code: "activation_disabled",
    message: "This artifact is not the active one for its scope",
    rule_id: null,
    step_id: null,
    source: null,
  },
];

describe("DiagnosticsBanner", () => {
  it("shows unknown commands, compile questions, stale contracts and disabled activations", () => {
    render(<DiagnosticsBanner diagnostics={ALL} />);
    const banner = within(screen.getByRole("region", { name: "Graph diagnostics" }));
    expect(banner.getByText(/is not registered/)).toBeInTheDocument();
    expect(banner.getByText(/Unresolved question on check-gate/)).toBeInTheDocument();
    expect(banner.getByText(/contract changed since this artifact was compiled/)).toBeInTheDocument();
    expect(banner.getByText(/not the active one for its scope/)).toBeInTheDocument();
    for (const code of ["unknown_command", "compile_question", "stale_contract", "activation_disabled"]) {
      expect(banner.getAllByText(code).length).toBeGreaterThan(0);
    }
  });

  it("orders errors before warnings, questions and info", () => {
    render(<DiagnosticsBanner diagnostics={ALL} />);
    const severities = screen
      .getAllByRole("listitem")
      .map((li) => li.querySelector("span")!.textContent);
    expect(severities).toEqual(["error", "warning", "question", "info"]);
  });

  it("links a diagnostic to the step it blames", async () => {
    const onSelectNode = vi.fn();
    render(<DiagnosticsBanner diagnostics={ALL} onSelectNode={onSelectNode} />);
    await userEvent.click(screen.getByRole("button", { name: "check-gate" }));
    expect(onSelectNode).toHaveBeenCalledWith("check-gate");
  });

  it("renders nothing at all when the artifact is clean", () => {
    const { container } = render(<DiagnosticsBanner diagnostics={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
