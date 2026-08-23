import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Section, Field } from "../FormSection";

describe("FormSection", () => {
  it("Section renders title, optional hint, and children", () => {
    render(
      <Section title="Basics" hint="some hint">
        <p>child content</p>
      </Section>,
    );
    expect(screen.getByText("Basics")).toBeInTheDocument();
    expect(screen.getByText("some hint")).toBeInTheDocument();
    expect(screen.getByText("child content")).toBeInTheDocument();
  });

  it("Field renders a label and children", () => {
    render(
      <Field label="Name">
        <input aria-label="Name" />
      </Field>,
    );
    expect(screen.getByText("Name")).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toBeInTheDocument();
  });
});
