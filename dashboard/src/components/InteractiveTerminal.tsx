import { useEffect, useId, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { connectTerminal, terminalDimensions, type TerminalConnection, type TerminalConnectionState } from "../ws/terminalSocket";
import { canFocusTerminal } from "./terminalFocus";
import { TerminalToolbar, TERMINAL_TOOL } from "./TerminalPane";
import { guardUntrustedOutput, TERMINAL_FONT_FAMILY, TERMINAL_LINE_HEIGHT, TERMINAL_THEME } from "./terminalSetup";

const encoder = new TextEncoder();

export default function InteractiveTerminal({ sessionId, name, focusRequest }: { sessionId: string; name: string; focusRequest?: string | null }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const focusButton = useRef<HTMLButtonElement>(null);
  const controlsRef = useRef<HTMLDivElement>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const connectionRef = useRef<TerminalConnection | null>(null);
  const handledFocusRequest = useRef<string | null>(null);
  const [state, setState] = useState<TerminalConnectionState>({ status: "connecting" });
  const hintId = useId();

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let disposed = false;
    let frame: number | null = null;
    const terminal = new Terminal({
      fontFamily: TERMINAL_FONT_FAMILY,
      fontSize: 12,
      lineHeight: TERMINAL_LINE_HEIGHT,
      cursorBlink: true,
      scrollback: 2000,
      disableStdin: true,
      logLevel: "off",
      theme: TERMINAL_THEME,
    });
    terminalRef.current = terminal;
    const fitAddon = new FitAddon();
    terminal.loadAddon(fitAddon);

    const disposables = guardUntrustedOutput(terminal);
    terminal.open(host);
    terminal.textarea?.setAttribute("aria-label", name + " terminal input");
    terminal.textarea?.setAttribute("aria-describedby", hintId);
    terminal.textarea?.setAttribute("aria-multiline", "true");
    terminal.attachCustomKeyEventHandler((event) => {
      if (event.ctrlKey && event.key.toLowerCase() === "m") {
        event.preventDefault();
        if (event.type === "keydown") {
          const target = terminal.options.disableStdin ? controlsRef.current : focusButton.current;
          target?.focus({ preventScroll: true });
        }
        return false;
      }
      if (terminal.options.disableStdin && event.key === "Tab") return false;
      // Let the browser's native copy event use xterm's selected text.
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "c" && terminal.hasSelection()) return false;
      return true;
    });

    const fit = () => {
      if (disposed) return;
      const bounds = host.getBoundingClientRect();
      connectionRef.current?.setVisible(!!bounds.width && !!bounds.height);
      if (!bounds.width || !bounds.height) return;
      const proposed = fitAddon.proposeDimensions();
      if (!proposed || !Number.isFinite(proposed.cols) || !Number.isFinite(proposed.rows)) return;
      const size = terminalDimensions(proposed.cols, proposed.rows);
      if (size.cols !== terminal.cols || size.rows !== terminal.rows) terminal.resize(size.cols, size.rows);
    };
    const scheduleFit = () => {
      if (disposed || frame !== null) return;
      frame = requestAnimationFrame(() => { frame = null; fit(); });
    };
    fit();
    const connection = connectTerminal({
      sessionId, cols: terminal.cols, rows: terminal.rows,
      visible: !!host.getBoundingClientRect().width && !!host.getBoundingClientRect().height,
      write: (bytes, processed) => terminal.write(bytes, processed),
      onState: (next) => {
        if (disposed) return;
        const disabled = next.status !== "connected";
        terminal.options.disableStdin = disabled;
        terminal.textarea?.setAttribute("aria-disabled", String(disabled));
        terminal.textarea?.setAttribute("aria-readonly", String(disabled));
        if (terminal.textarea) terminal.textarea.tabIndex = disabled ? -1 : 0;
        setState(next);
      },
    });
    connectionRef.current = connection;
    disposables.push(
      terminal.onData((data) => connection.sendInput(encoder.encode(data))),
      terminal.onBinary((data) => connection.sendInput(Uint8Array.from(data, (character) => character.charCodeAt(0) & 255))),
      terminal.onResize(({ cols, rows }) => connection.resize(cols, rows)),
    );
    const observer = new ResizeObserver(scheduleFit);
    observer.observe(host);
    document.fonts?.ready.then(scheduleFit);

    return () => {
      disposed = true;
      connection.close();
      observer.disconnect();
      if (frame !== null) cancelAnimationFrame(frame);
      disposables.forEach((subscription) => subscription.dispose());
      terminal.dispose();
      terminalRef.current = null;
      connectionRef.current = null;
    };
  }, [sessionId, name, hintId]);

  useEffect(() => {
    if (!focusRequest || state.status !== "connected" || handledFocusRequest.current === focusRequest) return;
    handledFocusRequest.current = focusRequest;
    if (canFocusTerminal()) terminalRef.current?.focus();
  }, [focusRequest, state.status]);

  const disabled = state.status !== "connected";
  const reconnect = state.status === "reconnecting";
  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <TerminalToolbar title={name} primary={
        <div ref={controlsRef} tabIndex={-1} className="flex shrink-0 items-center gap-1">
          <span id={hintId} className="sr-only">{disabled ? "Keyboard input unavailable until the terminal connects" : "Click to type · Ctrl+M releases keyboard"}</span>
          <span role="status" aria-label={name + " terminal connection"}
            title={reconnect ? `Reconnecting… (attempt ${state.attempt})` : state.message || state.status}>
            <span aria-hidden="true" className={"block h-2 w-2 rounded-full " + (disabled ? "bg-amber-400" : "bg-emerald-400")} />
            <span className="sr-only">{reconnect ? `Reconnecting… (attempt ${state.attempt})` : state.status}{disabled && " · input unavailable"}</span>
          </span>
          <button ref={focusButton} type="button" data-primary-control aria-label={"Focus " + name + " terminal"} disabled={disabled}
            onClick={() => terminalRef.current?.focus()} className={TERMINAL_TOOL}>Type</button>
        </div>
      } details={<>
        <p>Live tmux · interactive</p>
        <p>{disabled ? "Keyboard input unavailable until the terminal connects" : "Click to type · Ctrl+M releases keyboard"}</p>
        <div className="flex flex-wrap gap-2">
          <button type="button" data-primary-control aria-label={"Send Enter to " + name} disabled={disabled}
            onClick={() => connectionRef.current?.sendInput(encoder.encode("\r"))} className={TERMINAL_TOOL}>Enter</button>
          <button type="button" data-primary-control aria-label={"Interrupt " + name} disabled={disabled}
            onClick={() => connectionRef.current?.sendInput(encoder.encode("\x03"))} className={TERMINAL_TOOL}>Ctrl+C</button>
        </div>
        {state.message && <div role={state.status === "error" ? "alert" : "status"}>
          <p>{state.message}</p>
          {reconnect && <button type="button" data-primary-control onClick={() => connectionRef.current?.reconnect()} className={TERMINAL_TOOL}>Reconnect now</button>}
        </div>}
      </>} />
      <div className="min-h-0 min-w-0 flex-1 overflow-hidden bg-[#0d1117] p-2">
        <div ref={hostRef} data-interactive-terminal title={disabled ? "Keyboard input unavailable until the terminal connects" : "Click to type · Ctrl+M releases keyboard"}
          onKeyDown={(event) => event.stopPropagation()} className="h-full w-full [&_.xterm]:h-full [&_.xterm-viewport]:bg-[#0d1117]!" />
      </div>
    </div>
  );
}
