import { useEffect } from "react";

// A review may have multiple composers. One navigation prompts once for all of them.
const activeDrafts = new Set<symbol>();
let currentIndex: number | undefined;
let restoring = false;
function pop(event: PopStateEvent) {
  if (!activeDrafts.size) return;
  const nextIndex = event.state?.idx;
  if (restoring) { restoring = false; event.stopImmediatePropagation(); return; }
  if (typeof currentIndex !== "number" || typeof nextIndex !== "number" || nextIndex === currentIndex) return;
  if (!window.confirm("Leave this page and discard the unsaved draft?")) {
    event.stopImmediatePropagation();
    restoring = true;
    window.history.go(currentIndex - nextIndex);
  }
}

// Register before BrowserRouter: its pop listener can synchronously unmount composers.
export function installDraftNavigationGuard() {
  window.addEventListener("popstate", pop, true);
}

export function confirmDraftNavigation() {
  return !activeDrafts.size || window.confirm("Leave this page and discard the unsaved draft?");
}

function unload(event: BeforeUnloadEvent) { event.preventDefault(); event.returnValue = ""; }
function link(event: MouseEvent) {
  if (!(event.target instanceof Element)) return;
  const target = event.target.closest<HTMLAnchorElement>("a[href]");
  if (!target || target.download || target.target === "_blank" || target.getAttribute("href")?.startsWith("#")) return;
  if (!window.confirm("Leave this page and discard the unsaved draft?")) {
    event.preventDefault(); event.stopImmediatePropagation();
  }
}
/** Drafts live only in component memory; leaving requires an explicit choice. */
export function useDraftWarning(dirty: boolean) {
  useEffect(() => {
    if (!dirty) return;
    const id = Symbol();
    if (!activeDrafts.size) {
      currentIndex = window.history.state?.idx;
      window.addEventListener("beforeunload", unload);
      document.addEventListener("click", link, true);
    }
    activeDrafts.add(id);
    return () => {
      activeDrafts.delete(id);
      if (!activeDrafts.size) {
        window.removeEventListener("beforeunload", unload);
        document.removeEventListener("click", link, true);
      }
    };
  }, [dirty]);
}
