import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EdgeLegend } from "../EdgeLegend";
import { edgeStyleForType } from "../edgeStyle";

describe("EdgeLegend", () => {
  it("names the four drawn edge types by their dash pattern, then the arrow sentence", () => {
    const { container } = render(<EdgeLegend />);
    const legend = container.querySelector("[data-edge-legend]")!;
    expect(legend).toHaveTextContent(
      "blocks" + "waits for" + "conditional" + "discovered from"
      + "arrows point to the dependent task · ×N folds links from hidden tasks",
    );
    const dashes = [...legend.querySelectorAll("path")].map((path) => path.style.strokeDasharray);
    expect(dashes).toEqual(["", "10 4", "6 3", "2 4"]);
    expect(screen.getByText("arrows point to the dependent task · ×N folds links from hidden tasks")).toBeInTheDocument();
  });
});

describe("edgeStyleForType", () => {
  it("draws every type with the same neutral stroke, told apart by dash only", () => {
    for (const type of ["blocks", "waits-for", "conditional-blocks", "discovered-from"]) {
      expect(edgeStyleForType(type)).toMatchObject({ stroke: "var(--g-edge)", strokeWidth: 1.5 });
    }
    expect(edgeStyleForType("blocks").strokeDasharray).toBeUndefined();
  });
});
