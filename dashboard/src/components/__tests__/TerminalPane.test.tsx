import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useState } from "react";
import { Cog6ToothIcon, CommandLineIcon } from "@heroicons/react/24/outline";
import TerminalPane, { TerminalTabs, TerminalToolbar } from "../TerminalPane";

afterEach(cleanup);

const TABS = [
  { id: "terminal" as const, label: "Terminal", Icon: CommandLineIcon },
  { id: "settings" as const, label: "Settings", Icon: Cog6ToothIcon },
];

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

describe("Terminal pane view switch", () => {
  it("carries the terminal and settings tabs in the header, so changing views needs no disclosure", async () => {
    const user = userEvent.setup();
    function Pane() {
      const [tab, setTab] = useState<"terminal" | "settings">("terminal");
      return <TerminalPane title="Worker" onClose={vi.fn()}
        tabs={<TerminalTabs label="Worker view" idPrefix="pane" tabs={TABS} value={tab} onChange={setTab} />}
        details={<p>Long task and model details</p>}>
        <p>{tab === "terminal" ? "Terminal body" : "Settings body"}</p>
      </TerminalPane>;
    }
    const view = render(<Pane />);
    const header = view.container.querySelector("header")!;
    expect(within(header).getByRole("tablist", { name: "Worker view" })).toBeVisible();
    expect(within(header).getByRole("tab", { name: "Terminal" })).toHaveAttribute("aria-selected", "true");
    expect(within(header).getByRole("tab", { name: "Settings" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByText("Terminal body")).toBeVisible();

    await user.click(within(header).getByRole("tab", { name: "Settings" }));
    expect(screen.getByText("Settings body")).toBeVisible();
    expect(within(header).getByRole("tab", { name: "Settings" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("dialog")).toBeNull();

    // The old placement is gone: opening the details no longer repeats the switch.
    fireEvent.click(screen.getByRole("button", { name: "Details for Worker" }));
    const dialog = screen.getByRole("dialog", { name: "Worker details" });
    expect(within(dialog).queryByRole("tab")).toBeNull();
    expect(within(dialog).getByText("Long task and model details")).toBeVisible();
  });

  it("keeps each header tab a named, focusable control that a keyboard can activate", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<TerminalPane title="Worker" onClose={vi.fn()}
      tabs={<TerminalTabs label="Worker view" idPrefix="pane" tabs={TABS} value="terminal" onChange={onChange} />}
      details={<p>Details</p>}>
      <p>Terminal body</p>
    </TerminalPane>);
    const settings = screen.getByRole("tab", { name: "Settings" });
    // Icon-only at phone widths: the accessible name and tooltip carry the label.
    expect(settings).toHaveAttribute("title", "Settings");
    expect(settings).toHaveAttribute("aria-controls", "pane-panel");
    expect(settings).toHaveAttribute("data-primary-control");
    settings.focus();
    await user.keyboard("{Enter}");
    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange).toHaveBeenCalledWith("settings");
  });

  it("keeps the switch in the disclosure on a compact row, which has no room for it", () => {
    const width = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: 320 });
    try {
      const view = render(<TerminalPane title="Worker" onClose={vi.fn()}
        tabs={<TerminalTabs label="Worker view" idPrefix="pane" tabs={TABS} value="terminal" onChange={vi.fn()} />}
        details={<p>Long task and model details</p>}>
        <p>Terminal body</p>
      </TerminalPane>);
      expect(within(view.container.querySelector("header")!).queryByRole("tab")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Details for Worker" }));
      const dialog = screen.getByRole("dialog", { name: "Worker details" });
      expect(within(dialog).getByRole("tab", { name: "Terminal" })).toHaveAttribute("aria-selected", "true");
      expect(within(dialog).getByRole("tab", { name: "Settings" })).toBeInTheDocument();
    } finally {
      Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: width });
    }
  });
});
