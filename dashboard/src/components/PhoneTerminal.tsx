import { useEffect, useRef, useState, type CSSProperties } from "react";
import { Link } from "react-router-dom";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { ArrowDownIcon, ArrowsPointingInIcon, ArrowsPointingOutIcon } from "@heroicons/react/24/outline";
import TerminalInputBar from "./TerminalInputBar";
import TerminalKeyStrip from "./TerminalKeyStrip";
import { TerminalToolbar, TERMINAL_TOOL } from "./TerminalPane";
import { swallowAltScreen } from "./phoneTerminalStream";
import { attachTouchScroll } from "./terminalTouchScroll";
import { fitFontSize, guardUntrustedOutput, MIN_COLUMNS, TERMINAL_FONT_FAMILY, TERMINAL_LINE_HEIGHT, TERMINAL_THEME } from "./terminalSetup";
import type { TerminalKey } from "./terminalInput";
import { connectTerminal, terminalDimensions, type TerminalConnection, type TerminalConnectionState } from "../ws/terminalSocket";
import { usePaneStream, type PaneState } from "../ws/usePaneStream";
import { useTerminalTyping } from "../ws/useTerminalTyping";
import { useHistoryOverlay } from "../hooks/useHistoryOverlay";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { useVisualViewport } from "../hooks/useVisualViewport";

export const FONT_MIN = 12;
export const FONT_MAX = 20;
const FONT_STEP = 2;
/** tmux history the daemon puts ahead of the live screen, so earlier output is a scroll away. */
const HISTORY_LINES = 2000;
const SCROLLBACK_LINES = 8000;

const TOOL = TERMINAL_TOOL;
const encoder = new TextEncoder();
/** A pane snapshot replaces the whole screen; a snapshot is never scrollback. */
const SNAPSHOT = "\x1b[0m\x1b[?25l\x1b[H\x1b[2J\x1b[3J";

/**
 * One line for the terminal's state. It is a live region, so it changes on a
 * transition only — never per frame. A viewer the daemon will not attach
 * watches the pane stream instead.
 */
function statusText(state: TerminalConnectionState, pane: PaneState): string {
  if (state.status === "exited") return "Session ended — last screen";
  if (state.refused) {
    if (pane.status === "stopped") return "Session ended — last screen";
    // A stale screen says how old it is.
    const at = pane.lastFrameAt ? new Date(pane.lastFrameAt).toLocaleTimeString() : null;
    const since = at ? ` · screen from ${at}` : "";
    if (pane.status === "error") return `Watch only — stream error${since}`;
    if (pane.interrupted) return at ? `Watch only — reconnecting${since}` : "Watch only — reconnecting…";
    if (pane.status === "open") return "Watch only — typing is not available to this viewer";
    return "Watch only — connecting…";
  }
  if (state.status === "connecting") return "Connecting…";
  if (state.status === "reconnecting") return `Reconnecting… (attempt ${state.attempt ?? 1})`;
  if (state.status === "error") return state.message ?? "Terminal error";
  return "Live";
}

/**
 * A tap types into the input bar, never xterm's hidden textarea, so the
 * keyboard opens below the output. Called inside the tap's touchend, so iOS
 * raises the keyboard.
 */
function focusInput(input: HTMLTextAreaElement | null) {
  input?.focus({ preventScroll: true });
}

/** The fullscreen API is an extra: refused or missing, the CSS sheet still covers the viewport. */
function requestFullscreen(el: HTMLElement | null) {
  try {
    void el?.requestFullscreen?.().catch(() => {});
  } catch {
    // Unsupported (iPhone Safari has no element fullscreen).
  }
}

/**
 * The phone terminal (mobile terminal spec,
 * docs/superpowers/specs/2026-10-08-mobile-terminal-design.md): the same xterm,
 * theme and attach socket as the desktop terminal, sized to the phone. Its
 * columns and rows follow the viewport, rotation and the on-screen keyboard,
 * and tmux follows them; the agent's window gets its size back when the phone
 * leaves. The daemon puts tmux history ahead of the live screen, so a drag
 * scrolls back through earlier output. A tap opens the input bar and the
 * keyboard; there is no watch/type mode. A session that has ended, or a viewer
 * the daemon will not attach, is watch only.
 */
export default function PhoneTerminal({
  sessionId,
  name,
  focusHref = null,
}: {
  sessionId: string;
  name: string;
  /** A compact non-focus page links to the focus session instead of opening its own full screen. */
  focusHref?: string | null;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const connectionRef = useRef<TerminalConnection | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fitRef = useRef<(() => void) | null>(null);
  const [state, setState] = useState<TerminalConnectionState>({ status: "connecting" });
  const [fontSize, setFontSize] = useState(FONT_MIN);
  const preferredFont = useRef(fontSize);
  // The last finished fit: the size asked for and the size that kept MIN_COLUMNS.
  const [fitted, setFitted] = useState({ requested: fontSize, font: fontSize });
  const shownFont = fitted.font;
  const [scrolledBack, setScrolledBack] = useState(false);
  const [inputFocused, setInputFocused] = useState(false);
  const refused = !!state.refused;
  const readOnly = refused || state.status === "exited";
  const connected = state.status === "connected";
  const pane = usePaneStream(sessionId, { enabled: refused });
  const typing = useTerminalTyping(connectionRef, connected);
  const full = useHistoryOverlay(`terminal:${sessionId}`);
  const sheet = useRef<HTMLDivElement>(null);
  useFocusTrap(sheet, full.open, { onEscape: full.hide });
  // While the keyboard is up, fill exactly what it leaves visible: iOS does not
  // shrink the layout viewport, so an in-flow input bar would sit behind it.
  // The terminal refits to that space, and tmux with it.
  const viewport = useVisualViewport(inputFocused && !readOnly);
  const keyboard = viewport?.keyboard ? viewport : null;

  const readOnlyRef = useRef(readOnly);
  useEffect(() => { readOnlyRef.current = readOnly; }, [readOnly]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let disposed = false;
    let frame: number | null = null;
    const terminal = new Terminal({
      fontFamily: TERMINAL_FONT_FAMILY,
      fontSize: preferredFont.current,
      lineHeight: TERMINAL_LINE_HEIGHT,
      cursorBlink: false,
      scrollback: SCROLLBACK_LINES,
      disableStdin: true,
      logLevel: "off",
      theme: TERMINAL_THEME,
    });
    terminalRef.current = terminal;
    const fitAddon = new FitAddon();
    terminal.loadAddon(fitAddon);
    const disposables = [...guardUntrustedOutput(terminal), ...swallowAltScreen(terminal)];
    terminal.open(host);
    if (terminal.textarea) {
      terminal.textarea.tabIndex = -1;
      terminal.textarea.setAttribute("aria-hidden", "true");
    }

    const updateScrolled = () => {
      const buffer = terminal.buffer.active;
      setScrolledBack(buffer.viewportY < buffer.baseY);
    };
    const fit = () => {
      if (disposed) return;
      const bounds = host.getBoundingClientRect();
      connectionRef.current?.setVisible(!!bounds.width && !!bounds.height);
      if (!bounds.width || !bounds.height) return;
      const propose = () => {
        const proposed = fitAddon.proposeDimensions();
        return proposed && Number.isFinite(proposed.cols) && Number.isFinite(proposed.rows) ? proposed : null;
      };
      const font = fitFontSize(preferredFont.current, (size) => {
        if (terminal.options.fontSize !== size) terminal.options.fontSize = size;
        return propose()?.cols ?? null;
      });
      const proposed = propose();
      if (!proposed) return;
      const requested = preferredFont.current;
      setFitted((last) => (last.requested === requested && last.font === font ? last : { requested, font }));
      const size = terminalDimensions(proposed.cols, proposed.rows);
      if (size.cols !== terminal.cols || size.rows !== terminal.rows) terminal.resize(size.cols, size.rows);
    };
    const scheduleFit = () => {
      if (disposed || frame !== null) return;
      frame = requestAnimationFrame(() => { frame = null; fit(); });
    };
    fitRef.current = scheduleFit;
    fit();

    const connection = connectTerminal({
      sessionId, cols: terminal.cols, rows: terminal.rows,
      visible: !!host.getBoundingClientRect().width && !!host.getBoundingClientRect().height,
      history: HISTORY_LINES,
      restoreSize: true,
      write: (bytes, processed) => terminal.write(bytes, processed),
      onState: (next) => {
        if (disposed) return;
        // Answers to tmux's terminal queries go back as on the desktop; typing
        // goes through the input bar.
        terminal.options.disableStdin = next.status !== "connected";
        setState(next);
      },
    });
    connectionRef.current = connection;
    disposables.push(
      terminal.onData((data) => connection.sendInput(encoder.encode(data))),
      terminal.onBinary((data) => connection.sendInput(Uint8Array.from(data, (character) => character.charCodeAt(0) & 255))),
      terminal.onResize(({ cols, rows }) => connection.resize(cols, rows)),
      terminal.onScroll(updateScrolled),
      terminal.onWriteParsed(updateScrolled),
    );
    const detachTouch = attachTouchScroll(host, {
      lineHeight: () => {
        const screen = host.querySelector<HTMLElement>(".xterm-screen");
        const height = screen?.getBoundingClientRect().height ?? 0;
        return height && terminal.rows ? height / terminal.rows : (terminal.options.fontSize ?? FONT_MIN) * TERMINAL_LINE_HEIGHT;
      },
      scroll: (lines) => terminal.scrollLines(lines),
      onTap: () => { if (!readOnlyRef.current) focusInput(inputRef.current); },
    });
    const observer = new ResizeObserver(scheduleFit);
    observer.observe(host);
    document.fonts?.ready.then(scheduleFit);

    return () => {
      disposed = true;
      connection.close();
      detachTouch();
      observer.disconnect();
      if (frame !== null) cancelAnimationFrame(frame);
      disposables.forEach((subscription) => subscription.dispose());
      terminal.dispose();
      terminalRef.current = null;
      connectionRef.current = null;
      fitRef.current = null;
    };
  }, [sessionId]);

  useEffect(() => {
    preferredFont.current = fontSize;
    fitRef.current?.();
  }, [fontSize]);

  // A viewer the daemon will not attach watches the pane stream, drawn by the
  // same terminal so it keeps the same colours.
  useEffect(() => {
    if (!refused || pane.screen === null) return;
    terminalRef.current?.write(SNAPSHOT + pane.screen.replace(/\r?\n/g, "\r\n"));
  }, [refused, pane.screen]);

  useEffect(() => {
    if (!full.open && sheet.current && document.fullscreenElement === sheet.current) {
      void document.exitFullscreen().catch(() => {});
    }
  }, [full.open]);

  const enterFull = () => {
    full.show();
    requestFullscreen(sheet.current); // inside the tap's user gesture
  };
  // What is typed lands at the bottom, so show it.
  const sendEntry = (text: string) => {
    terminalRef.current?.scrollToBottom();
    typing.sendEntry(text);
  };
  const sendKey = (key: TerminalKey | string) => {
    terminalRef.current?.scrollToBottom();
    typing.sendKey(key);
  };
  // A mouse click is a tap too, unless it ended a selection.
  const clickTerminal = () => {
    if (readOnly || terminalRef.current?.hasSelection()) return;
    focusInput(inputRef.current);
  };
  const status = statusText(state, pane);
  const healthy = refused ? pane.status === "open" && !pane.interrupted : connected;
  // Judged on a finished fit, so a tap's pending refit never turns Larger off.
  const capped = fitted.font < fitted.requested && fontSize >= fitted.requested;
  const keyboardStyle: CSSProperties | undefined = keyboard
    ? { top: keyboard.top, left: keyboard.left, width: keyboard.width, height: keyboard.height }
    : undefined;

  return (
    <div
      ref={sheet}
      tabIndex={-1}
      role={full.open ? "dialog" : undefined}
      aria-modal={full.open || undefined}
      aria-label={full.open ? `${name} terminal, full screen` : undefined}
      data-keyboard-open={keyboard ? "" : undefined}
      style={keyboardStyle}
      className={
        keyboard
          ? "fixed z-50 flex flex-col bg-[#0d1117] px-safe outline-none"
          : full.open
            ? "fixed inset-0 z-50 flex flex-col bg-[#0d1117] pt-safe pb-safe px-safe outline-none"
            : "flex h-full min-h-0 flex-1 flex-col outline-none"
      }
    >
      <TerminalToolbar title={name} standalone={full.open || !!keyboard} primary={<>
        <span role="status" aria-label={`${name} terminal status`} title={status}>
          <span aria-hidden="true" className={"block h-2 w-2 rounded-full " + (healthy ? "bg-emerald-400" : "bg-amber-400")} />
          <span className="sr-only">{status}</span>
        </span>
        {focusHref ? (
          <Link to={focusHref} data-primary-control aria-label="Full screen" className={TOOL}>
            <ArrowsPointingOutIcon aria-hidden="true" className="h-4 w-4" />
          </Link>
        ) : (
          // One button for both states, so focus stays on it across the switch.
          <button
            type="button"
            data-primary-control
            aria-label={full.open ? "Exit full screen" : "Full screen"}
            className={TOOL}
            onClick={full.open ? full.hide : enterFull}
          >
            {full.open ? <ArrowsPointingInIcon aria-hidden="true" className="h-4 w-4" /> : <ArrowsPointingOutIcon aria-hidden="true" className="h-4 w-4" />}
          </button>
        )}
      </>} details={<>
        <p>{status}</p>
        <div className="flex flex-wrap items-center gap-1">
          <button
            type="button"
            data-primary-control
            aria-label="Smaller text"
            className={TOOL}
            disabled={fontSize <= FONT_MIN}
            onClick={() => setFontSize((size) => Math.max(FONT_MIN, Math.min(size, shownFont) - FONT_STEP))}
          >
            A−
          </button>
          <output aria-label="Text size" className="w-10 shrink-0 text-center text-xs text-gray-400">
            {shownFont}px
          </output>
          <button
            type="button"
            data-primary-control
            aria-label="Larger text"
            className={TOOL}
            disabled={fontSize >= FONT_MAX || capped}
            onClick={() => setFontSize((size) => Math.min(FONT_MAX, size + FONT_STEP))}
          >
            A+
          </button>
        </div>
        {capped && <p>Text is {shownFont}px here to keep {MIN_COLUMNS} columns.</p>}
        {state.status === "reconnecting" && (
          <button type="button" data-primary-control className={TOOL} onClick={() => connectionRef.current?.reconnect()}>
            Reconnect now
          </button>
        )}
        {refused && (pane.interrupted || pane.status === "error") && (
          <button type="button" data-primary-control className={TOOL} onClick={pane.reconnect}>
            Retry
          </button>
        )}
      </>} />
      <div className="relative min-h-0 min-w-0 flex-1 overflow-hidden bg-[#0d1117] p-1">
        {/* xterm clips each row to its grid; across a landscape row the DOM
            renderer's glyphs can run a pixel or two past it (data-allow-overflow-x
            for the layout checks). The page never scrolls sideways. */}
        <div
          ref={hostRef}
          role="region"
          aria-label={`${name} terminal output`}
          data-phone-terminal
          data-allow-overflow-x
          onClick={clickTerminal}
          className="h-full w-full touch-none overscroll-contain [&_.xterm]:h-full [&_.xterm-viewport]:bg-[#0d1117]!"
        />
        {scrolledBack && (
          <button
            type="button"
            aria-label="Jump to latest output"
            className="absolute bottom-3 right-3 flex h-11 w-11 items-center justify-center rounded-full border border-gray-700 bg-gray-900/90 text-gray-100 shadow"
            onClick={() => terminalRef.current?.scrollToBottom()}
          >
            <ArrowDownIcon aria-hidden="true" className="h-5 w-5" />
          </button>
        )}
      </div>
      {!readOnly && (
        <>
          <TerminalKeyStrip name={name} disabled={!connected} onKey={sendKey} />
          <TerminalInputBar
            name={name}
            connected={connected}
            onSubmit={sendEntry}
            inputRef={inputRef}
            onFocusChange={setInputFocused}
          />
        </>
      )}
    </div>
  );
}
