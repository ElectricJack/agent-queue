import { useEffect, useState } from "react";

export interface VisualViewportRect {
  top: number;
  left: number;
  width: number;
  height: number;
  /** The on-screen keyboard (or other browser UI) covers part of the layout viewport. */
  keyboard: boolean;
}

/** Less than this is browser chrome settling, not a keyboard. */
const KEYBOARD_MIN_PX = 80;

function measure(vv: VisualViewport): VisualViewportRect {
  return {
    top: vv.offsetTop,
    left: vv.offsetLeft,
    width: vv.width,
    height: vv.height,
    // A pinch zoom also shrinks the visual viewport; only an unzoomed one is a keyboard.
    keyboard: Math.abs(vv.scale - 1) < 0.01 && window.innerHeight - vv.height > KEYBOARD_MIN_PX,
  };
}

/**
 * The visible rectangle while `enabled`, updated once per frame. iOS Safari
 * does not shrink the layout viewport for the keyboard, so a fixed element
 * sized to this rectangle is what stays above the keyboard. Null where the
 * browser has no `visualViewport`, or while disabled.
 */
export function useVisualViewport(enabled: boolean): VisualViewportRect | null {
  const [rect, setRect] = useState<VisualViewportRect | null>(null);
  useEffect(() => {
    const vv = typeof window === "undefined" ? undefined : window.visualViewport;
    if (!enabled || !vv) return;
    let frame: number | null = null;
    const read = () => {
      frame = null;
      setRect(measure(vv));
    };
    const schedule = () => {
      if (frame === null) frame = requestAnimationFrame(read);
    };
    schedule();
    vv.addEventListener("resize", schedule);
    vv.addEventListener("scroll", schedule);
    return () => {
      if (frame !== null) cancelAnimationFrame(frame);
      vv.removeEventListener("resize", schedule);
      vv.removeEventListener("scroll", schedule);
      setRect(null);
    };
  }, [enabled]);
  return enabled ? rect : null;
}
