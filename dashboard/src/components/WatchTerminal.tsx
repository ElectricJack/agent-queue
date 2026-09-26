import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowsPointingInIcon, ArrowsPointingOutIcon } from "@heroicons/react/24/outline";
import LivePaneConsole from "./LivePaneConsole";
import { usePaneStream, type PaneState } from "../ws/usePaneStream";
import { useHistoryOverlay } from "../hooks/useHistoryOverlay";
import { useFocusTrap } from "../hooks/useFocusTrap";

export const FONT_MIN = 12;
export const FONT_MAX = 20;
const FONT_STEP = 2;

const TOOL =
  "inline-flex shrink-0 items-center justify-center gap-1 rounded border border-gray-700 px-2 text-xs text-gray-200 hover:bg-gray-800 disabled:opacity-40";

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

/** The fullscreen API is an extra (spec §4.2): refused or missing, the CSS sheet still covers the viewport. */
function requestFullscreen(el: HTMLElement | null) {
  try {
    void el?.requestFullscreen?.().catch(() => {});
  } catch {
    // Unsupported (iPhone Safari has no element fullscreen).
  }
}

/**
 * The watch-only terminal (mobile dashboard §4): the read-only live pane, a
 * 12–20 px font control that changes rendering only, and a full-screen view
 * that is a history-aware, focus-trapped sheet over the whole viewport, so it
 * survives rotation. It sends nothing to the session — no input, no resize —
 * and never mounts InteractiveTerminal or opens a terminal WebSocket.
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
  const full = useHistoryOverlay(`terminal:${sessionId}`);
  const sheet = useRef<HTMLDivElement>(null);
  useFocusTrap(sheet, full.open, { onEscape: full.hide });

  useEffect(() => {
    if (!full.open && document.fullscreenElement && document.fullscreenElement === sheet.current) {
      void document.exitFullscreen().catch(() => {});
    }
  }, [full.open]);

  const enterFull = () => {
    full.show();
    requestFullscreen(sheet.current); // inside the tap's user gesture
  };
  const stale = stream.interrupted || stream.status === "error";

  return (
    <div
      ref={sheet}
      tabIndex={-1}
      role={full.open ? "dialog" : undefined}
      aria-modal={full.open || undefined}
      aria-label={full.open ? `${name} terminal, full screen` : undefined}
      className={
        full.open
          ? "fixed inset-0 z-50 flex flex-col bg-black pt-safe pb-safe px-safe outline-none"
          : "flex min-h-0 flex-1 flex-col outline-none"
      }
    >
      <div className="flex shrink-0 flex-wrap items-center gap-1 border-b border-gray-800 bg-gray-950 px-2 py-1">
        {/* Its own row on a narrow phone, so the controls never squeeze it away. */}
        <span
          role="status"
          aria-label={`${name} terminal status`}
          className="min-w-0 grow basis-full truncate text-xs text-gray-400 sm:basis-0"
        >
          {statusText(stream)}
        </span>
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
        {stale && (
          <button type="button" data-primary-control className={TOOL} onClick={stream.reconnect}>
            Retry
          </button>
        )}
        {focusHref ? (
          <Link to={focusHref} data-primary-control aria-label="Full screen" className={TOOL}>
            <ArrowsPointingOutIcon className="h-4 w-4" />
            <span>Full screen</span>
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
            {full.open ? <ArrowsPointingInIcon className="h-4 w-4" /> : <ArrowsPointingOutIcon className="h-4 w-4" />}
            <span>{full.open ? "Exit full screen" : "Full screen"}</span>
          </button>
        )}
      </div>
      <LivePaneConsole
        screen={stream.screen}
        status={stream.status}
        error={stream.error}
        fontSize={fontSize}
        stale={stale}
        className="min-h-0 flex-1"
      />
    </div>
  );
}
