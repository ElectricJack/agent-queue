import { useEffect, useId, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { connectTerminal, terminalDimensions, type TerminalConnection, type TerminalConnectionState } from "../ws/terminalSocket";

const encoder = new TextEncoder();

export default function InteractiveTerminal({ sessionId, name }: { sessionId: string; name: string }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const focusButton = useRef<HTMLButtonElement>(null);
  const controlsRef = useRef<HTMLDivElement>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const connectionRef = useRef<TerminalConnection | null>(null);
  const [state, setState] = useState<TerminalConnectionState>({ status: "connecting" });
  const [attempt, setAttempt] = useState(0);
  const hintId = useId();

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let disposed = false;
    let frame: number | null = null;
    const terminal = new Terminal({
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
      fontSize: 12,
      lineHeight: 1.2,
      cursorBlink: true,
      scrollback: 2000,
      disableStdin: true,
      logLevel: "off",
      theme: { background: "#0d1117", foreground: "#d1d5db", cursor: "#e5e7eb", selectionBackground: "#6366f14d" },
    });
    terminalRef.current = terminal;
    const fitAddon = new FitAddon();
    terminal.loadAddon(fitAddon);

    // Terminal output is untrusted. Keep manual copy/paste, but never let
    // escape sequences access the clipboard or activate remote hyperlinks.
    const disposables = [
      terminal.parser.registerOscHandler(52, () => true),
      terminal.parser.registerOscHandler(8, () => true),
    ];
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
  }, [sessionId, name, attempt, hintId]);

  const disabled = state.status !== "connected";
  const reconnect = state.status === "disconnected" || state.status === "error";
  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      {/* One control row only: the terminal itself owns every other pixel of height. */}
      <div ref={controlsRef} tabIndex={-1} className="flex shrink-0 items-center gap-2 border-b border-gray-800 px-3 py-1 text-[10px] text-gray-500">
        <span>Live tmux · interactive</span>
        <span id={hintId} className="sr-only">Click to type · Ctrl+M releases keyboard</span>
        <span role="status" aria-label={name + " terminal connection"} className="ml-auto capitalize">{state.status}</span>
        <button ref={focusButton} type="button" aria-label={"Focus " + name + " terminal"} disabled={disabled}
          onClick={() => terminalRef.current?.focus()}
          className="rounded border border-gray-700 px-1.5 py-0.5 text-gray-300 hover:bg-gray-800 disabled:opacity-40">Type</button>
        <button type="button" aria-label={"Send Enter to " + name} disabled={disabled}
          onClick={() => connectionRef.current?.sendInput(encoder.encode("\r"))}
          className="rounded border border-gray-700 px-1.5 py-0.5 text-gray-300 hover:bg-gray-800 disabled:opacity-40">Enter</button>
        <button type="button" aria-label={"Interrupt " + name} disabled={disabled}
          onClick={() => connectionRef.current?.sendInput(encoder.encode("\x03"))}
          className="rounded border border-gray-700 px-1.5 py-0.5 text-gray-300 hover:bg-gray-800 disabled:opacity-40">Ctrl+C</button>
      </div>
      <div className="min-h-0 min-w-0 flex-1 overflow-hidden bg-[#0d1117] p-2">
        <div ref={hostRef} title="Click to type · Ctrl+M releases keyboard"
          onKeyDown={(event) => event.stopPropagation()} className="h-full w-full [&_.xterm]:h-full" />
      </div>
      {state.message && (
        <div role={reconnect ? "alert" : "status"} className="shrink-0 border-t border-gray-800 px-3 py-2 text-xs text-gray-300">
          <p>{state.message}</p>
          {reconnect && (
            <button type="button" onClick={() => setAttempt((value) => value + 1)}
              className="mt-2 rounded border border-gray-700 px-2 py-1 hover:bg-gray-800">
              Reconnect terminal
            </button>
          )}
        </div>
      )}
    </div>
  );
}
