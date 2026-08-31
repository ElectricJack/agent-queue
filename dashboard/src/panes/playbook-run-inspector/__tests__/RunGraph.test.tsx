import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import RunGraph, { buildRunGraph } from "../RunGraph";

const graph = {
  id: "default-pipeline",
  pipeline_rules: {
    "task.completed": [{ entry: "route-start", when: { field: "status", equals: "done" } }],
  },
  nodes: {
    "route-start": {
      entry: true,
      action: { command: "ensure_task", on_success: "route-finish", on_failure: "route-failed" },
    },
    "route-finish": { terminal: true },
    "route-failed": { terminal: true },
  },
};

describe("buildRunGraph", () => {
  it("keeps every node and labels pipeline success/failure edges", () => {
    const built = buildRunGraph(graph, "route-start", []);
    expect(built.nodes.map((node) => node.id)).toEqual(["route-start", "route-finish", "route-failed"]);
    expect(built.edges.map((edge) => [edge.source, edge.target, edge.label])).toEqual([
      ["route-start", "route-finish", "on_success"],
      ["route-start", "route-failed", "on_failure"],
    ]);
    expect(built.nodes.find((node) => node.id === "route-start")?.data.current).toBe(true);
  });
});

describe("RunGraph", () => {
  it("renders rules, all nodes, edge meanings, and the current node", () => {
    render(<RunGraph graph={graph} currentNode="route-start" trace={[]} />);
    const rules = screen.getByTestId("playbook-rules");
    expect(within(rules).getByText("task.completed")).toBeInTheDocument();
    expect(within(rules).getByText(/route-start/)).toBeInTheDocument();
    expect(within(rules).getByText(/status/)).toBeInTheDocument();
    expect(screen.getAllByTestId("playbook-graph-node")).toHaveLength(3);
    expect(document.getElementById("playbook-node-route-start")).toHaveAttribute("data-current", "true");
    expect(screen.getByText("on_success")).toBeInTheDocument();
    expect(screen.getByText("on_failure")).toBeInTheDocument();
  });
});
