import { createContext, useContext, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { EllipsisHorizontalIcon, XMarkIcon } from "@heroicons/react/24/outline";

export const TERMINAL_TOOL = "inline-flex h-8 min-w-8 shrink-0 items-center justify-center gap-1 rounded px-2 text-xs text-gray-300 hover:bg-gray-800 focus-visible:outline-2 focus-visible:outline-indigo-400 disabled:opacity-40";

const Chrome = createContext<{ primary: HTMLDivElement | null; details: HTMLDivElement | null } | null>(null);

function TerminalHeader({ title, status, primary, details, onClose, titleId }: {
  title: string; status?: ReactNode; primary?: ReactNode; details: ReactNode;
  onClose?: () => void; titleId?: string;
}) {
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const id = useId();

  useLayoutEffect(() => {
    if (!open) return;
    const position = () => {
      if (!button.current || !panel.current) return;
      const anchor = button.current.getBoundingClientRect();
      const bounds = panel.current.getBoundingClientRect();
      panel.current.style.left = Math.max(8, Math.min(anchor.right - bounds.width, window.innerWidth - bounds.width - 8)) + "px";
      panel.current.style.top = Math.max(8, Math.min(anchor.bottom + 4, window.innerHeight - bounds.height - 8)) + "px";
    };
    position();
    panel.current?.focus({ preventScroll: true });
    const observer = new ResizeObserver(position);
    if (panel.current) observer.observe(panel.current);
    window.addEventListener("resize", position);
    window.addEventListener("scroll", position, true);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", position);
      window.removeEventListener("scroll", position, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !panel.current?.contains(event.target) && !button.current?.contains(event.target)) setOpen(false);
    };
    // Capture before a fullscreen sheet's Escape handler: first dismiss details.
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      button.current?.focus({ preventScroll: true });
    };
    document.addEventListener("pointerdown", outside, true);
    window.addEventListener("keydown", escape, true);
    return () => {
      document.removeEventListener("pointerdown", outside, true);
      window.removeEventListener("keydown", escape, true);
    };
  }, [open]);

  return <>
    <header data-terminal-header className="flex min-w-0 shrink-0 items-center gap-1 whitespace-nowrap border-b border-gray-800 bg-gray-900 px-2 py-0.5">
      <h2 id={titleId} title={title} className="min-w-0 flex-1 truncate text-xs font-semibold text-gray-100">{title}</h2>
      {status && <span className="max-w-24 shrink truncate text-[10px] capitalize text-gray-400">{status}</span>}
      {primary}
      <button ref={button} type="button" data-primary-control aria-label={"Details for " + title}
        aria-haspopup="dialog" aria-expanded={open} aria-controls={id} title="Terminal details and controls"
        onClick={() => setOpen(!open)} className={TERMINAL_TOOL}>
        <EllipsisHorizontalIcon aria-hidden="true" className="h-4 w-4" />
      </button>
      {onClose && <button type="button" data-primary-control aria-label={"Close " + title + " view"}
        title="Close view (session keeps running)" onClick={onClose} className={TERMINAL_TOOL}>
        <XMarkIcon aria-hidden="true" className="h-4 w-4" />
      </button>}
    </header>
    {open && <div ref={panel} id={id} role="dialog" aria-label={title + " details"} tabIndex={-1}
      data-terminal-details
      onKeyDown={(event) => event.stopPropagation()}
      onBlur={(event) => {
        if (event.relatedTarget instanceof Node && !event.currentTarget.contains(event.relatedTarget) && event.relatedTarget !== button.current) setOpen(false);
      }}
      className="fixed z-[60] max-h-[calc(100dvh-1rem)] w-[min(24rem,calc(100vw-1rem))] space-y-3 overflow-y-auto rounded-lg border border-gray-700 bg-gray-900 p-3 text-xs text-gray-300 shadow-xl outline-none [overflow-wrap:anywhere]">
      <div className="flex items-center gap-2">
        <h3 className="min-w-0 flex-1 font-semibold">{title}</h3>
        <button type="button" data-primary-control aria-label={"Dismiss " + title + " details"} className={TERMINAL_TOOL}
          onClick={() => { setOpen(false); button.current?.focus({ preventScroll: true }); }}>
          <XMarkIcon aria-hidden="true" className="h-4 w-4" />
        </button>
      </div>
      {details}
    </div>}
  </>;
}

/** A window and its terminal share one header; portals add transport controls without remounting xterm. */
export default function TerminalPane({ title, status, primary, details, onClose, titleId, children }: {
  title: string; status?: ReactNode; primary?: ReactNode; details: ReactNode; onClose: () => void; titleId?: string; children: ReactNode;
}) {
  const [primaryTarget, setPrimary] = useState<HTMLDivElement | null>(null);
  const [extra, setExtra] = useState<HTMLDivElement | null>(null);
  return <Chrome.Provider value={{ primary: primaryTarget, details: extra }}>
    <TerminalHeader title={title} status={status} onClose={onClose} titleId={titleId}
      primary={<>{primary}<div ref={setPrimary} className="flex min-w-0 shrink-0 items-center gap-1" /></>}
      details={<>{details}<div ref={setExtra} className="space-y-2" /></>} />
    {children}
  </Chrome.Provider>;
}

/** Standalone/focus terminals get the same chrome; tiled terminals populate their window's header. */
export function TerminalToolbar({ title, primary, details, standalone = false }: {
  title: string; primary: ReactNode; details: ReactNode; standalone?: boolean;
}) {
  const chrome = useContext(Chrome);
  if (chrome && !standalone) return <>
    {chrome.primary && createPortal(primary, chrome.primary)}
    {chrome.details && createPortal(details, chrome.details)}
  </>;
  return <TerminalHeader title={title} primary={primary} details={details} />;
}
