import { useEffect, useId, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { canFocusTerminal } from "./terminalFocus";
import { swallowAltScreen } from "./phoneTerminalStream";
import { attachTouchScroll } from "./terminalTouchScroll";
import { guardUntrustedOutput, TERMINAL_FONT_SIZE, TERMINAL_LINE_HEIGHT, terminalOptions } from "./terminalSetup";
import { TerminalToolbar, TERMINAL_TOOL } from "./TerminalPane";
import { connectTerminal, terminalDimensions, type TerminalConnection, type TerminalConnectionState } from "../ws/terminalSocket";

/** tmux scrollback the daemon puts ahead of the live screen, so earlier output is a scroll away. */
const HISTORY_LINES = 2000;
/** Below this the shell is one column with drawers, and so is every terminal's host. */
const PHONE_QUERY = "(max-width: 767.98px)";

const encoder = new TextEncoder();

/**
 * A session's terminal, and the only terminal in the dashboard.
 *
 * The host shell page's remote shell, the agent flock's windows, a pool
 * instance, the session detail page, the focus session page and the session
 * pane all render this component, so an agent terminal is the same window on a
 * phone as on a desktop — same xterm, same options and palette
 * (`terminalOptions()`), same fit logic, same attach socket, same input path.
 * There is no phone variant to drift from it.
 *
 * A compact viewport differs only in what it asks tmux for, which is invisible
 * in the window and is what makes a phone usable: `history` lines of scrollback
 * ahead of the live screen, and `restore_size` so the agent's window gets its
 * own size back when the phone leaves. Touch dragging reaches that scrollback,
 * and an on-screen keyboard caps the terminal to the space it leaves, so tmux's
 * size is always the size on show.
 */
export default function InteractiveTerminal({ sessionId, name, focusRequest }: { sessionId: string; name: string; focusRequest?: string | null }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<HTMLDivElement>(null);
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
    const compact = typeof window !== "undefined"
      && typeof window.matchMedia === "function"
      && window.matchMedia(PHONE_QUERY).matches;
    let disposed = false;
    let frame: number | null = null;
    const terminal = new Terminal(terminalOptions(TERMINAL_FONT_SIZE));
    terminalRef.current = terminal;
    const fitAddon = new FitAddon();
    terminal.loadAddon(fitAddon);
    // Scrollback only exists if tmux stays in the normal buffer, so the two
    // travel together: a terminal that asked for history gets the normal
    // buffer, and one that did not leaves full-screen programs alone.
    const disposables = [
      ...guardUntrustedOutput(terminal),
      ...(compact ? swallowAltScreen(terminal) : []),
    ];
    terminal.open(host);
    if (terminal.textarea) {
      terminal.textarea.setAttribute("aria-label", name + " terminal input");
      terminal.textarea.setAttribute("aria-describedby", hintId);
      terminal.textarea.setAttribute("aria-multiline", "true");
    }
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
      history: compact ? HISTORY_LINES : 0,
      restoreSize: compact,
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
    // xterm.js 6 bundles VS Code's touch gestures but never registers a target,
    // so a finger on the terminal scrolls nothing — or the page. A drag here
    // scrolls the terminal's own buffer by whole lines, keeps going with a
    // decaying momentum, and keeps the gesture from the page; a touch that does
    // not move is a tap, and types.
    const detachTouch = attachTouchScroll(host, {
      lineHeight: () => {
        const screen = host.querySelector<HTMLElement>(".xterm-screen");
        const height = screen?.getBoundingClientRect().height ?? 0;
        return height && terminal.rows ? height / terminal.rows : (terminal.options.fontSize ?? TERMINAL_FONT_SIZE) * TERMINAL_LINE_HEIGHT;
      },
      scroll: (lines) => terminal.scrollLines(lines),
      onTap: () => terminal.focus(),
    });
    const observer = new ResizeObserver(scheduleFit);
    observer.observe(host);
    document.fonts?.ready.then(scheduleFit);
    // An on-screen keyboard covers the terminal without changing the layout
    // viewport (iOS), so the space left for it is only visible in
    // `visualViewport`. Cap the terminal to what the keyboard leaves: without
    // this the rows on show stay the rows behind the keyboard, and tmux keeps
    // sizing a window nobody can read. Off a device with a keyboard this never
    // moves, so a desktop terminal is untouched.
    const viewport = window.visualViewport;
    const fitViewport = () => {
      const frame = frameRef.current;
      if (frame) {
        // The terminal can begin well below the page header (a session detail
        // or a docked pane). Only the part between its top and the keyboard
        // fits, rather than an entire visual viewport of terminal rows.
        // With no keyboard, keep the host shell's layout intact, including
        // terminals below the fold in a scrolling flock workspace.
        const bottom = viewport && Math.abs(viewport.scale - 1) < 0.01
          && viewport.height < window.innerHeight - 1
          ? viewport.offsetTop + viewport.height : Infinity;
        frame.style.maxHeight = bottom === Infinity ? ""
          : `${Math.max(0, Math.floor(bottom - frame.getBoundingClientRect().top))}px`;
      }
      scheduleFit();
    };
    viewport?.addEventListener("resize", fitViewport);
    viewport?.addEventListener("scroll", fitViewport);
    window.addEventListener("scroll", fitViewport, true);
    fitViewport();

    return () => {
      disposed = true;
      connection.close();
      detachTouch();
      observer.disconnect();
      viewport?.removeEventListener("resize", fitViewport);
      viewport?.removeEventListener("scroll", fitViewport);
      window.removeEventListener("scroll", fitViewport, true);
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
  const hint = disabled ? "Keyboard input unavailable until the terminal connects" : "Click to type · Ctrl+M releases keyboard";
  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <TerminalToolbar title={name} primary={
        <div ref={controlsRef} tabIndex={-1} className="flex shrink-0 items-center gap-1">
          <span id={hintId} className="sr-only">{hint}</span>
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
        <p>{hint}</p>
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
      {/* xterm clips each row to its grid; across a landscape row the DOM
          renderer's glyphs can run a pixel or two past it (data-allow-overflow-x
          for the layout checks). The page never scrolls sideways. */}
      <div ref={frameRef} className="relative min-h-0 min-w-0 flex-1 overflow-hidden bg-[#0d1117] p-2">
        <div
          ref={hostRef}
          data-interactive-terminal
          data-allow-overflow-x
          title={hint}
          onKeyDown={(event) => event.stopPropagation()}
          className="h-full w-full touch-none overscroll-contain [&_.xterm]:h-full [&_.xterm-viewport]:bg-[#0d1117]!"
        />
      </div>
    </div>
  );
}
