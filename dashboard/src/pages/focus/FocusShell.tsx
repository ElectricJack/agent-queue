import { Suspense, useLayoutEffect, useRef, useState } from "react";
import { Link, Outlet, useLocation, useNavigationType } from "react-router-dom";
import { ArrowLeftIcon, ArrowTopRightOnSquareIcon, HomeIcon } from "@heroicons/react/24/outline";
import { TerminalLinkModeProvider } from "../../components/terminalLinks";
import ConnectionBanner from "../../components/ConnectionBanner";
import { FocusChromeProvider, type FocusChrome } from "./focusChrome";
import { useFocusBack } from "./useFocusBack";
import { FOCUS_ROOT } from "./routes";

/** Scroll offset per history entry, so Back returns to the same place in a list. */
const scrollByEntry = new Map<string, number>();
const SCROLL_ENTRIES = 100;

function remember(key: string, top: number) {
  scrollByEntry.delete(key);
  scrollByEntry.set(key, top);
  if (scrollByEntry.size > SCROLL_ENTRIES) {
    const oldest = scrollByEntry.keys().next().value;
    if (oldest !== undefined) scrollByEntry.delete(oldest);
  }
}

// Every action shows its label (mobile dashboard §4.1: no hover-only
// affordance); the accessible name contains the visible text.
const HEADER_BUTTON = "inline-flex shrink-0 items-center justify-center gap-1 rounded px-2 text-sm text-gray-300 hover:bg-gray-800";

/**
 * The focus routes' shell (mobile dashboard §4.1): edge-to-edge at every width,
 * outside the three-column grid, inside the app's query/router/event/state
 * providers. It never reads or writes the roaming right surface (§4.2), and its
 * terminal links open the phone terminal.
 */
export default function FocusShell() {
  const location = useLocation();
  const navigationType = useNavigationType();
  const back = useFocusBack();
  const main = useRef<HTMLElement>(null);
  const [chrome, setChrome] = useState<FocusChrome>({ title: "Agent Q" });
  const home = location.pathname === FOCUS_ROOT;

  // Back/Forward restore the entry's scroll once the page is tall enough;
  // cached queries render at once, so a few frames suffice.
  useLayoutEffect(() => {
    const el = main.current;
    if (!el) return;
    const target = navigationType === "POP" ? scrollByEntry.get(location.key) ?? 0 : 0;
    let frames = 0;
    let id = 0;
    const apply = () => {
      el.scrollTop = target;
      if (Math.abs(el.scrollTop - target) > 1 && frames++ < 30) id = requestAnimationFrame(apply);
    };
    apply();
    return () => cancelAnimationFrame(id);
  }, [location.key, navigationType]);

  return (
    <TerminalLinkModeProvider mode="watch">
      <FocusChromeProvider onChange={setChrome}>
        <div data-focus-shell className="app-viewport flex w-full flex-col bg-gray-950 text-gray-100">
          <header className="shrink-0 border-b border-gray-800 pt-safe px-safe">
            <div className="flex items-center gap-1 px-1">
              {!home && (
                <button type="button" data-primary-control aria-label="Back" onClick={back} className={HEADER_BUTTON}>
                  <ArrowLeftIcon className="h-5 w-5" /><span>Back</span>
                </button>
              )}
              <h1 className="min-w-0 flex-1 truncate px-2 text-base font-semibold" title={chrome.title}>
                {chrome.title}
              </h1>
              {!home && (
                <Link to={FOCUS_ROOT} data-primary-control aria-label="Focus home" className={HEADER_BUTTON}>
                  <HomeIcon className="h-5 w-5" /><span>Home</span>
                </Link>
              )}
              {chrome.fullHref && (
                <Link to={chrome.fullHref} data-primary-control aria-label="Open in full dashboard" className={HEADER_BUTTON}>
                  <ArrowTopRightOnSquareIcon className="h-5 w-5" /><span>Full</span>
                </Link>
              )}
            </div>
          </header>
          <ConnectionBanner />
          <main
            ref={main}
            onScroll={(event) => remember(location.key, event.currentTarget.scrollTop)}
            className="min-h-0 flex-1 overflow-y-auto pb-safe px-safe"
          >
            <Suspense fallback={<p role="status" className="p-4 text-sm text-gray-500">Loading…</p>}>
              <Outlet />
            </Suspense>
          </main>
        </div>
      </FocusChromeProvider>
    </TerminalLinkModeProvider>
  );
}
