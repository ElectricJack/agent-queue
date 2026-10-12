/**
 * Touch scrolling for an xterm.js terminal. xterm.js 6 bundles VS Code's touch
 * gestures but never registers a target, so a finger on the terminal scrolls
 * nothing — or the page. A drag here scrolls the terminal's own buffer by
 * whole lines, keeps going with a decaying momentum after release, and keeps
 * the gesture from the page. A touch that does not move is a tap.
 */

/** Movement under this is a tap that wobbled, not a drag. */
const TAP_SLOP_PX = 8;
/** A press longer than this is not a tap (it may be the start of a text selection). */
const TAP_MAX_MS = 600;
/** Velocity is measured over the last stretch of the drag only. */
const VELOCITY_WINDOW_MS = 100;
/** Momentum decays like iOS's: e^(-t / 325 ms). */
const DECAY_MS = 325;
/** px/ms below which a release or a glide stops. */
const MIN_VELOCITY = 0.05;

export interface TouchScrollOptions {
  /** The height of one terminal row in CSS pixels. */
  lineHeight: () => number;
  /** Scroll by whole lines: negative is up, toward earlier output. */
  scroll: (lines: number) => void;
  /** A tap: called inside `touchend`, so it may focus an input and raise the keyboard. */
  onTap: () => void;
}

export function attachTouchScroll(element: HTMLElement, { lineHeight, scroll, onTap }: TouchScrollOptions): () => void {
  let startX = 0;
  let startY = 0;
  let startAt = 0;
  let lastY = 0;
  let tracking = false;
  let dragging = false;
  let pixels = 0;
  let samples: { at: number; y: number }[] = [];
  let frame: number | null = null;

  const stopGlide = () => {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
  };
  const move = (dy: number) => {
    const height = lineHeight();
    if (!(height > 0)) return;
    pixels += dy;
    const lines = Math.trunc(pixels / height);
    if (!lines) return;
    pixels -= lines * height;
    scroll(lines);
  };
  const glide = (velocity: number) => {
    let last = performance.now();
    const step = (now: number) => {
      const elapsed = Math.max(0, now - last);
      last = now;
      move(velocity * elapsed);
      velocity *= Math.exp(-elapsed / DECAY_MS);
      frame = Math.abs(velocity) < MIN_VELOCITY ? null : requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
  };

  const onStart = (event: TouchEvent) => {
    stopGlide();
    tracking = event.touches.length === 1;
    dragging = false;
    if (!tracking) return;
    const touch = event.touches[0]!;
    startX = touch.clientX;
    startY = lastY = touch.clientY;
    startAt = event.timeStamp;
    pixels = 0;
    samples = [{ at: event.timeStamp, y: touch.clientY }];
  };
  const onMove = (event: TouchEvent) => {
    if (!tracking) return;
    if (event.touches.length !== 1) { tracking = false; return; }
    const touch = event.touches[0]!;
    if (!dragging && Math.hypot(touch.clientX - startX, touch.clientY - startY) < TAP_SLOP_PX) return;
    dragging = true;
    // The terminal owns the gesture: no page scroll, no pull-to-refresh.
    if (event.cancelable) event.preventDefault();
    // A finger moving down drags earlier output into view.
    move(lastY - touch.clientY);
    lastY = touch.clientY;
    samples.push({ at: event.timeStamp, y: touch.clientY });
    while (samples.length > 2 && event.timeStamp - samples[0]!.at > VELOCITY_WINDOW_MS) samples.shift();
  };
  const onEnd = (event: TouchEvent) => {
    if (!tracking) return;
    tracking = false;
    if (!dragging) {
      if (event.timeStamp - startAt > TAP_MAX_MS) return;
      // No synthesized mousedown: xterm.js would focus its own textarea.
      if (event.cancelable) event.preventDefault();
      onTap();
      return;
    }
    const first = samples[0]!;
    const last = samples[samples.length - 1]!;
    const elapsed = last.at - first.at;
    const velocity = elapsed > 0 && event.timeStamp - last.at < VELOCITY_WINDOW_MS ? (first.y - last.y) / elapsed : 0;
    if (Math.abs(velocity) >= MIN_VELOCITY) glide(velocity);
  };
  const onCancel = () => { tracking = false; dragging = false; };

  const options: AddEventListenerOptions = { capture: true, passive: false };
  element.addEventListener("touchstart", onStart, options);
  element.addEventListener("touchmove", onMove, options);
  element.addEventListener("touchend", onEnd, options);
  element.addEventListener("touchcancel", onCancel, options);
  return () => {
    stopGlide();
    element.removeEventListener("touchstart", onStart, options);
    element.removeEventListener("touchmove", onMove, options);
    element.removeEventListener("touchend", onEnd, options);
    element.removeEventListener("touchcancel", onCancel, options);
  };
}
