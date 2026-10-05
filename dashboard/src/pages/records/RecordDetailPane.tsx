import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowLeftIcon, ArrowTopRightOnSquareIcon, XMarkIcon } from "@heroicons/react/24/outline";

/** Independent scrolling, accessible resizing, and a modal sheet on narrow screens. */
export default function RecordDetailPane({ children, fullHref, from, onClose, sheet }: {
  children: ReactNode; fullHref: string | null; from: string; onClose: () => void; sheet: boolean;
}) {
  const paneRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const drag = useRef<{ x: number; width: number } | null>(null);
  const [width, setWidth] = useState(440);
  const [maxWidth, setMaxWidth] = useState(700);
  useEffect(() => {
    const parent = paneRef.current?.parentElement;
    if (!parent || sheet) return;
    const measure = () => setMaxWidth(Math.max(320, Math.min(800, parent.clientWidth - 280)));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(parent);
    return () => observer.disconnect();
  }, [sheet]);
  useEffect(() => {
    if (sheet) closeRef.current?.focus({ preventScroll: true });
  }, [sheet]);
  const currentWidth = Math.min(width, maxWidth);
  const resize = (value: number) => setWidth(Math.max(320, Math.min(maxWidth, value)));
  return <aside ref={paneRef} aria-label="Record detail" role={sheet ? "dialog" : "complementary"}
    aria-modal={sheet || undefined} data-record-detail data-layout={sheet ? "sheet" : "pane"}
    style={sheet ? undefined : { width: currentWidth }}
    className={sheet ? "fixed inset-0 z-50 flex min-h-0 flex-col bg-gray-950 pt-safe pb-safe px-safe" : "relative flex min-h-0 shrink-0 flex-col border-l border-gray-700 bg-gray-950"}
    onKeyDown={(event) => {
      if (!sheet || event.key !== "Tab" || (event.target as HTMLElement).closest('[role="dialog"]') !== paneRef.current) return;
      const controls = Array.from(paneRef.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]',
      ) ?? []).filter((el) => el.tabIndex >= 0 && el.getClientRects().length > 0);
      const first = controls[0]; const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }}>
    {!sheet && <div role="separator" aria-label="Resize record detail" aria-orientation="vertical"
      tabIndex={0} aria-valuemin={320} aria-valuemax={maxWidth} aria-valuenow={currentWidth}
      className="absolute inset-y-0 -left-1 z-10 w-2 cursor-col-resize touch-none hover:bg-indigo-500/40 focus:bg-indigo-500/40 focus:outline-none"
      onPointerDown={(event) => {
        drag.current = { x: event.clientX, width: currentWidth };
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        if (drag.current) resize(drag.current.width + drag.current.x - event.clientX);
      }}
      onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}
      onKeyDown={(event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault(); event.stopPropagation();
        resize(event.key === "Home" ? 320 : event.key === "End" ? maxWidth : currentWidth + (event.key === "ArrowLeft" ? 32 : -32));
      }} />}
    <header className="flex shrink-0 items-center justify-between gap-2 border-b border-gray-800 p-2">
      <button ref={closeRef} type="button" onClick={onClose} aria-label={sheet ? "Back to list" : "Close detail"}
        className="inline-flex min-h-11 items-center gap-2 rounded px-3 text-sm text-gray-300 hover:bg-gray-800">
        {sheet ? <><ArrowLeftIcon className="h-4 w-4" />Back</> : <><XMarkIcon className="h-4 w-4" />Close</>}
      </button>
      {fullHref && <Link to={fullHref} state={{ from }} className="inline-flex min-h-11 items-center gap-2 rounded px-3 text-sm text-indigo-300 hover:bg-gray-800">
        Open full page<ArrowTopRightOnSquareIcon className="h-4 w-4" />
      </Link>}
    </header>
    <div className="min-h-0 flex-1 overflow-y-auto [overflow-wrap:anywhere]">{children}</div>
  </aside>;
}
