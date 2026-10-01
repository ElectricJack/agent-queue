import { useEffect, useRef, useState, type CSSProperties } from "react";
import { Link } from "react-router-dom";
import { ArrowsPointingInIcon, ArrowsPointingOutIcon, EyeIcon, PencilSquareIcon } from "@heroicons/react/24/outline";
import LivePaneConsole from "./LivePaneConsole";
import TerminalInputBar from "./TerminalInputBar";
import TerminalKeyStrip from "./TerminalKeyStrip";
import { TerminalToolbar, TERMINAL_TOOL } from "./TerminalPane";
import { usePaneStream, type PaneState } from "../ws/usePaneStream";
import { useTerminalInput, type TerminalInput } from "../ws/useTerminalInput";
import { useHistoryOverlay } from "../hooks/useHistoryOverlay";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { useVisualViewport } from "../hooks/useVisualViewport";

export const FONT_MIN = 12;
export const FONT_MAX = 20;
const FONT_STEP = 2;

const TOOL = TERMINAL_TOOL;
const MODE = TERMINAL_TOOL;
const MODE_ON = " bg-gray-200 font-medium text-gray-950";
const MODE_OFF = " text-gray-300 hover:bg-gray-800";

/**
 * One line for the stream's state. It is a live region, so it changes on a
 * transition only — never per frame or per retry — and a stale state says how
 * old the screen on show is. The stream retries a drop on its own, with
 * backoff, until the session ends or the daemon refuses it.
 */
function statusText(stream: PaneState): string {
  const at = stream.lastFrameAt ? new Date(stream.lastFrameAt).toLocaleTimeString() : null;
  const since = at ? ` · screen from ${at}` : "";
  if (stream.status === "stopped") return "Session ended — last screen";
  if (stream.status === "error") return `Stream error${since}`;
  if (stream.interrupted) return at ? `Reconnecting${since}` : "Reconnecting…";
  if (stream.status === "connecting") return "Connecting…";
  if (stream.status === "closed") return "Not connected";
  return "Live";
}

/** The input socket's state, only while typing; connected says nothing (the Type button does). */
function inputStatusText(input: TerminalInput): string {
  const { state } = input;
  if (state.status === "connected" || state.status === "closed") return "";
  if (state.status === "connecting") return "Connecting the keyboard…";
  if (state.status === "reconnecting") return `Keyboard reconnecting… (attempt ${state.attempt ?? 1})`;
  return state.message ?? "Typing is unavailable.";
}

/** The fullscreen API is an extra (spec §4.2): refused or missing, the CSS sheet still covers the viewport. */
function requestFullscreen(el: HTMLElement | null) {
  try {
    void el?.requestFullscreen?.().catch(() => {});
  } catch {
    // Unsupported (iPhone Safari has no element fullscreen).
  }
}

/**
 * The phone terminal (mobile dashboard §4; mobile interactive terminal spec,
 * docs/superpowers/specs/2026-09-26-mobile-interactive-terminal-design.md):
 * the live pane stream, a 12–20 px font control that changes rendering only,
 * and a full-screen view that is a history-aware, focus-trapped sheet over the
 * whole viewport, so it survives rotation.
 *
 * It opens watch-only on every mount; the choice is never remembered. **Type**
 * adds an input bar and a key strip and opens the input-only terminal socket,
 * which types into the session without attaching, so the agent's tmux window
 * is never resized. Watch only closes it again. It never mounts
 * InteractiveTerminal and never attaches.
 */
export default function WatchTerminal({
  sessionId,
  name,
  focusHref = null,
}: {
  sessionId: string;
  name: string;
  /** A compact non-focus page links to the focus session instead of opening its own full screen. */
  focusHref?: string | null;
}) {
  const stream = usePaneStream(sessionId);
  const [fontSize, setFontSize] = useState(FONT_MIN);
  const [typing, setTyping] = useState(false);
  const [inputFocused, setInputFocused] = useState(false);
  const input = useTerminalInput(sessionId, typing);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const full = useHistoryOverlay(`terminal:${sessionId}`);
  const sheet = useRef<HTMLDivElement>(null);
  useFocusTrap(sheet, full.open, { onEscape: full.hide });
  // While the keyboard is up, fill exactly what it leaves visible: iOS does not
  // shrink the layout viewport, so an in-flow input bar would sit behind it.
  const viewport = useVisualViewport(typing && inputFocused);
  const keyboard = viewport?.keyboard ? viewport : null;

  useEffect(() => {
    if (!full.open && sheet.current && document.fullscreenElement === sheet.current) {
      void document.exitFullscreen().catch(() => {});
    }
  }, [full.open]);

  const enterFull = () => {
    full.show();
    requestFullscreen(sheet.current); // inside the tap's user gesture
  };
  const stale = stream.interrupted || stream.status === "error";
  // A tap on the screen types into the input bar, never a hidden terminal
  // textarea, so the keyboard opens below the output. A selection is not a tap.
  const focusInput = () => {
    if (window.getSelection()?.toString()) return;
    inputRef.current?.focus({ preventScroll: true });
  };
  const keyboardStyle: CSSProperties | undefined = keyboard
    ? { top: keyboard.top, left: keyboard.left, width: keyboard.width, height: keyboard.height }
    : undefined;
  const inputStatus = typing ? inputStatusText(input) : "";

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
          ? "fixed z-50 flex flex-col bg-black px-safe outline-none"
          : full.open
            ? "fixed inset-0 z-50 flex flex-col bg-black pt-safe pb-safe px-safe outline-none"
            : "flex h-full min-h-0 flex-1 flex-col outline-none"
      }
    >
      <TerminalToolbar title={name} standalone={full.open || !!keyboard} primary={<>
        <span role="status" aria-label={`${name} terminal status`} title={statusText(stream)}>
          <span aria-hidden="true" className={"block h-2 w-2 rounded-full " + (stale || stream.status !== "open" ? "bg-amber-400" : "bg-emerald-400")} />
          <span className="sr-only">{statusText(stream)}</span>
        </span>
        <button type="button" data-primary-control aria-label={typing ? "Watch only" : "Type"}
          aria-description={typing ? "Typing is active. Switch to watch only." : "Watch only. Enable typing."} className={MODE + (typing ? MODE_ON : MODE_OFF)}
          onClick={() => { setTyping(!typing); setInputFocused(false); }}>
          {typing ? <EyeIcon aria-hidden="true" className="h-4 w-4" /> : <PencilSquareIcon aria-hidden="true" className="h-4 w-4" />}
          <span>{typing ? "Watch" : "Type"}</span>
        </button>
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
        <p>{statusText(stream)}</p>
        <div className="flex flex-wrap items-center gap-1">
          <button
            type="button"
            data-primary-control
            aria-label="Smaller text"
            className={TOOL}
            disabled={fontSize <= FONT_MIN}
            onClick={() => setFontSize((size) => Math.max(FONT_MIN, size - FONT_STEP))}
          >
            A−
          </button>
          <output aria-label="Text size" className="w-10 shrink-0 text-center text-xs text-gray-400">
            {fontSize}px
          </output>
          <button
            type="button"
            data-primary-control
            aria-label="Larger text"
            className={TOOL}
            disabled={fontSize >= FONT_MAX}
            onClick={() => setFontSize((size) => Math.min(FONT_MAX, size + FONT_STEP))}
          >
            A+
          </button>
        </div>
        {stale && (
          <button type="button" data-primary-control className={TOOL} onClick={stream.reconnect}>
            Retry
          </button>
        )}
      </>} />
      <div className="flex min-h-0 flex-1 flex-col" onClick={typing ? focusInput : undefined}>
        <LivePaneConsole
          screen={stream.screen}
          status={stream.status}
          error={stream.error}
          fontSize={fontSize}
          stale={stale}
          followBottom={typing}
          className="min-h-0 flex-1"
        />
      </div>
      {typing && (
        <>
          <div className={inputStatus ? "flex shrink-0 items-center gap-2 border-t border-gray-800 bg-gray-950 px-2 py-1 text-xs text-amber-300" : "sr-only"}>
            <span role="status" aria-label={`${name} keyboard status`} className="min-w-0 flex-1">{inputStatus}</span>
            {input.state.status === "reconnecting" && (
              <button type="button" data-primary-control className={TOOL} onClick={input.reconnect}>
                Reconnect now
              </button>
            )}
          </div>
          <TerminalKeyStrip name={name} disabled={!input.connected} onKey={input.sendKey} />
          <TerminalInputBar
            name={name}
            connected={input.connected}
            onSubmit={input.sendEntry}
            inputRef={inputRef}
            onFocusChange={setInputFocused}
          />
        </>
      )}
    </div>
  );
}
