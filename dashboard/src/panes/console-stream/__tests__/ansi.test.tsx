import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { ansiToSpans, stripAnsi } from "../ansi";

describe("ansiToSpans", () => {
  it("renders plain text with no ANSI codes as a single span", () => {
    const { container } = render(<>{ansiToSpans("plain text")}</>);
    expect(container.textContent).toBe("plain text");
  });

  it("applies color from an SGR code and resets after code 0", () => {
    const text = "\x1b[32mgreen\x1b[0m plain";
    const { container } = render(<>{ansiToSpans(text)}</>);
    expect(container.textContent).toBe("green plain");
    const spans = container.querySelectorAll("span");
    expect(spans[0].style.color).not.toBe("");
    expect(spans[1].style.color).toBe("");
  });

  it("applies bold from SGR code 1", () => {
    const text = "\x1b[1mbold\x1b[0m";
    const { container } = render(<>{ansiToSpans(text)}</>);
    const span = container.querySelector("span")!;
    expect(span.style.fontWeight).toBe("bold");
  });
});

describe("stripAnsi", () => {
  it("removes SGR escape codes, leaving plain text", () => {
    expect(stripAnsi("\x1b[32mgreen\x1b[0m plain")).toBe("green plain");
  });

  it("is a no-op on text with no escape codes", () => {
    expect(stripAnsi("plain")).toBe("plain");
  });
});
