import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import TerminalPane, { TerminalToolbar } from "../TerminalPane";

afterEach(cleanup);

describe("Terminal pane disclosure", () => {
  it("combines window and transport controls into one header without remounting the terminal", async () => {
    const mounted = vi.fn();
    const disposed = vi.fn();
    const enter = vi.fn();
    function Transport() {
      useEffect(() => { mounted(); return disposed; }, []);
      return <>
        <TerminalToolbar title="Builder" primary={<button>Type</button>} details={<button onClick={enter}>Enter</button>} />
        <textarea aria-label="Terminal input" />
      </>;
    }
    const view = render(<TerminalPane title="Builder" status="Running" onClose={vi.fn()} details={<p>Long task and model details</p>}><Transport /></TerminalPane>);
    expect(view.container.querySelectorAll("header")).toHaveLength(1);
    expect(within(view.container.querySelector("header")!).getByRole("button", { name: "Type" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Enter" })).toBeNull();
    const trigger = screen.getByRole("button", { name: "Details for Builder" });
    const user = userEvent.setup();
    trigger.focus();
    await user.keyboard("{Enter}");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("dialog", { name: "Builder details" })).toHaveFocus();
    expect(screen.getByText("Long task and model details")).toBeVisible();
    expect(screen.getByRole("button", { name: "Enter" })).toBeVisible();
    await user.tab(); // dismiss control
    await user.tab(); // the transport action rendered through the portal
    expect(screen.getByRole("button", { name: "Enter" })).toHaveFocus();
    await user.keyboard(" ");
    expect(enter).toHaveBeenCalledOnce();
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mounted).toHaveBeenCalledOnce();
    expect(disposed).not.toHaveBeenCalled();
  });

  it("opens by touch/click and lets an outside terminal click take focus without sending a close", () => {
    const close = vi.fn();
    render(<TerminalPane title="Worker" onClose={close} details={<button>Settings</button>}><textarea aria-label="Terminal input" /></TerminalPane>);
    fireEvent.click(screen.getByRole("button", { name: "Details for Worker" }));
    expect(screen.getByRole("button", { name: "Settings" })).toBeVisible();
    const input = screen.getByRole("textbox");
    fireEvent.pointerDown(input);
    input.focus();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(input).toHaveFocus();
    expect(close).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Close Worker view" }));
    expect(close).toHaveBeenCalledOnce();
  });

  it("dismisses on keyboard focus leaving details, and has standalone terminal chrome", () => {
    render(<><TerminalToolbar title="Worker" primary={<button>Type</button>} details={<button>Reconnect</button>} /><button>Outside</button></>);
    fireEvent.click(screen.getByRole("button", { name: "Details for Worker" }));
    fireEvent.blur(screen.getByRole("dialog"), { relatedTarget: screen.getByRole("button", { name: "Outside" }) });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getAllByRole("heading", { name: "Worker" })).toHaveLength(1);
  });
});
