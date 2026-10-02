import { useLayoutEffect, useRef, useState, type CSSProperties, type RefObject } from "react";

type Rect = Pick<DOMRect, "left" | "right" | "top" | "bottom">;

export type CommentReference = {
  container: HTMLElement;
  element: HTMLElement;
  rect: () => Rect;
  contextRect: () => Rect;
};

const GAP = 8;
const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(value, max));

/** All measurements and fixed positioning share viewport coordinates, outside pane containment. */
export function useCommentPosition(
  reference: CommentReference,
  floatingRef: RefObject<HTMLElement | null>,
  { withinContainer = false }: { withinContainer?: boolean } = {},
): CSSProperties {
  const [style, setStyle] = useState<CSSProperties>({ position: "fixed", visibility: "hidden" });
  const lastRects = useRef<{ anchor: Rect; context: Rect } | null>(null);

  useLayoutEffect(() => {
    const floating = floatingRef.current;
    if (!floating) return;
    let frame = 0;
    const update = () => {
      const viewport = window.visualViewport;
      const pane = reference.container.getBoundingClientRect();
      const viewportLeft = viewport?.offsetLeft ?? 0;
      const viewportTop = viewport?.offsetTop ?? 0;
      const viewportRight = viewportLeft + (viewport?.width ?? window.innerWidth);
      const viewportBottom = viewportTop + (viewport?.height ?? window.innerHeight);
      const left = (withinContainer ? Math.max(viewportLeft, pane.left) : viewportLeft) + GAP;
      const top = (withinContainer ? Math.max(viewportTop, pane.top) : viewportTop) + GAP;
      const right = (withinContainer ? Math.min(viewportRight, pane.right) : viewportRight) - GAP;
      const bottom = (withinContainer ? Math.min(viewportBottom, pane.bottom) : viewportBottom) - GAP;
      const widthLimit = Math.max(0, right - left);
      let heightLimit = Math.max(0, bottom - top);
      // Constrain before measuring: narrow screens, zoom and the keyboard can change wrapping.
      floating.style.maxWidth = `${widthLimit}px`;
      floating.style.maxHeight = `${heightLimit}px`;
      const width = floating.getBoundingClientRect().width;
      let height = floating.getBoundingClientRect().height;
      if (reference.element.isConnected || !lastRects.current) {
        lastRects.current = { anchor: reference.rect(), context: reference.contextRect() };
      }
      const { anchor, context } = lastRects.current;
      // Only protect context currently visible in the document's scrolling viewport.
      const protectedRect = {
        left: Math.max(left, pane.left, context.left),
        right: Math.min(right, pane.right, context.right),
        top: Math.max(top, pane.top, context.top),
        bottom: Math.min(bottom, pane.bottom, context.bottom),
      };
      const anchorX = clamp(anchor.left, left, right);
      const anchorY = clamp(anchor.top, top, bottom);
      const placements = () => [
        { left: protectedRect.right + GAP, top: anchorY },
        { left: protectedRect.left - GAP - width, top: anchorY },
        { left: anchorX, top: protectedRect.bottom + GAP },
        { left: anchorX, top: protectedRect.top - GAP - height },
      ].map((point) => {
        const x = clamp(point.left, left, right - width);
        const y = clamp(point.top, top, bottom - height);
        const overlap = Math.max(0, Math.min(x + width, protectedRect.right) - Math.max(x, protectedRect.left)) *
          Math.max(0, Math.min(y + height, protectedRect.bottom) - Math.max(y, protectedRect.top));
        return { left: x, top: y, overlap };
      });
      const choose = () => placements().reduce((a, b) => b.overlap < a.overlap ? b : a);
      let best = choose();
      if (best.overlap > 0) {
        // Short screens/keyboards: scrolling the editor can preserve context better than covering it.
        const freeHeight = Math.max(protectedRect.top - top - GAP, bottom - protectedRect.bottom - GAP);
        if (freeHeight >= 96 && freeHeight < height) {
          heightLimit = freeHeight;
          floating.style.maxHeight = `${heightLimit}px`;
          height = floating.getBoundingClientRect().height;
          best = choose();
        }
      }
      setStyle({
        position: "fixed", left: best.left, top: best.top,
        maxWidth: widthLimit, maxHeight: heightLimit, visibility: "visible",
      });
    };
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(update);
    };
    update();
    // Capture sees internal pane and ancestor scrolls, which do not bubble.
    document.addEventListener("scroll", schedule, true);
    window.addEventListener("resize", schedule);
    const viewport = window.visualViewport;
    viewport?.addEventListener("resize", schedule);
    viewport?.addEventListener("scroll", schedule);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
    observer?.observe(floating);
    observer?.observe(reference.container);
    observer?.observe(reference.element);
    if (reference.container.firstElementChild) observer?.observe(reference.container.firstElementChild);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("scroll", schedule, true);
      window.removeEventListener("resize", schedule);
      viewport?.removeEventListener("resize", schedule);
      viewport?.removeEventListener("scroll", schedule);
      observer?.disconnect();
    };
  }, [reference, floatingRef, withinContainer]);

  return style;
}
