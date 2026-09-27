import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import LivePaneConsole from "../LivePaneConsole";

describe("LivePaneConsole", () => {
  it("renders the screen text", () => {
    render(<LivePaneConsole screen={"line one\nline two"} status="open" />);
    expect(screen.getByText(/line one/)).toBeInTheDocument();
  });

  it("shows a waiting message before the first frame", () => {
    render(<LivePaneConsole screen={null} status="connecting" />);
    expect(screen.getByText(/waiting for pane/i)).toBeInTheDocument();
  });

  it("shows the error message on an error status", () => {
    render(
      <LivePaneConsole screen={null} status="error" error="tmux is gone" />,
    );
    expect(screen.getByText(/tmux is gone/)).toBeInTheDocument();
  });

  it("labels a stopped session while keeping the last screen", () => {
    render(<LivePaneConsole screen="final screen" status="stopped" />);
    expect(screen.getByText(/final screen/)).toBeInTheDocument();
    expect(screen.getByText(/session ended/i)).toBeInTheDocument();
  });

  it("renders at a given font size, dims a stale screen and scrolls sideways inside itself", () => {
    const { container } = render(
      <LivePaneConsole screen="text" status="open" fontSize={16} stale />,
    );
    const box = container.firstElementChild as HTMLElement;
    expect(box).toHaveStyle({ fontSize: "16px" });
    expect(box).not.toHaveClass("text-xs");
    expect(box).toHaveClass("opacity-60");
    expect(box).toHaveAttribute("data-allow-overflow-x");
  });
});
