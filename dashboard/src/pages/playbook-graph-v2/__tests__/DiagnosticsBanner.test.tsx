import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import DiagnosticsBanner from "../DiagnosticsBanner";
import { graph } from "./fixtures";

afterEach(cleanup);

const ALL = [
  ...graph.diagnostics!,
  { severity: "question" as const, code: "compile_question", message: "Unresolved question on check-gate", rule_id: null, step_id: "check-gate", source: null },
  { severity: "error" as const, code: "invalid_reference", message: "downstream.tasks is not declared", rule_id: null, step_id: "for-each-task", source: null },
];

describe("DiagnosticsBanner", () => {
  it("shows compile questions, invalid references, stale contracts and disabled activations", () => {
    render(<DiagnosticsBanner diagnostics={ALL} />);
    const banner = within(screen.getByRole("region", { name: "Graph diagnostics" }));
    expect(banner.getByText(/is not declared/)).toBeInTheDocument();
    expect(banner.getByText(/Unresolved question on check-gate/)).toBeInTheDocument();
    expect(banner.getByText(/contract changed since this artifact was compiled/)).toBeInTheDocument();
    expect(banner.getByText(/not the active one for its scope/)).toBeInTheDocument();
    for (const code of ["invalid_reference", "compile_question", "stale_contract", "activation_disabled"]) {
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
