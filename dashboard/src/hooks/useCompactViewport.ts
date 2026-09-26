import { useMediaQuery } from "./useMediaQuery";

/**
 * Below 768 CSS px the shell is one column with drawers and full-screen
 * surfaces (mobile dashboard spec §4.1). A presentation breakpoint, never a
 * device guess; Tailwind's `md:` (≥768 px) is its complement.
 */
export const COMPACT_VIEWPORT_QUERY = "(max-width: 767.98px)";

/** For decisions taken once, at first render (restore suppression). */
export function isCompactViewport(): boolean {
  return typeof window !== "undefined"
    && typeof window.matchMedia === "function"
    && window.matchMedia(COMPACT_VIEWPORT_QUERY).matches;
}

export function useCompactViewport(): boolean {
  return useMediaQuery(COMPACT_VIEWPORT_QUERY);
}
