import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import HostShell from "./HostShell";

const state = vi.hoisted(() => ({
  list: { data: undefined as unknown, isError: false, error: null as unknown },
  open: vi.fn(),
  close: vi.fn(),
}));

vi.mock("../../api/hostShell", () => ({
  useHostShells: () => state.list,
  useOpenHostShell: () => ({ mutate: state.open, isPending: false, error: null }),
  useCloseHostShell: () => ({ mutate: state.close, isPending: false, error: null }),
}));
vi.mock("../../components/InteractiveTerminal", () => ({
  default: ({ sessionId }: { sessionId: string }) => <div data-testid="terminal">{sessionId}</div>,
}));

afterEach(() => { cleanup(); state.open.mockReset(); state.close.mockReset(); });

describe("HostShell page", () => {
  it("explains the config flag when host shells are off", () => {
    state.list = { data: { enabled: false, shells: [] }, isError: false, error: null };
    render(<HostShell />);
    expect(screen.getByText(/dashboard.host_shell.enabled/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /open host shell/i })).not.toBeInTheDocument();
  });

  it("refuses non-operators with an alert", () => {
    state.list = { data: undefined, isError: true, error: new Error("403") };
    render(<HostShell />);
    expect(screen.getByRole("alert")).toHaveTextContent(/local operator/);
  });

  it("opens, reattaches to and closes shells", async () => {
    state.list = { data: { enabled: true, shells: [
      { name: "aq-host-shell-1", created_at: 1, attached_clients: 0 },
      { name: "aq-host-shell-2", created_at: 2, attached_clients: 0 },
    ] }, isError: false, error: null };
    render(<HostShell />);
    expect(await screen.findByTestId("terminal")).toHaveTextContent("aq-host-shell-1");
    await userEvent.click(screen.getByRole("tab", { name: "aq-host-shell-2" }));
    expect(screen.getByTestId("terminal")).toHaveTextContent("aq-host-shell-2");
    await userEvent.click(screen.getByRole("button", { name: /open host shell/i }));
    expect(state.open).toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Close aq-host-shell-2" }));
    expect(state.close).toHaveBeenCalledWith("aq-host-shell-2");
  });
});
