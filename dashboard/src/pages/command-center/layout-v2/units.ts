import { NODE_HEIGHT, NODE_WIDTH } from "../types";

export const UNIT_W = NODE_WIDTH;
export const UNIT_H = NODE_HEIGHT;
export const CELL = 8;
export const NODE_BUDGET = 400;
export type Rect = { x0: number; y0: number; x1: number; y1: number };
export type CellKey = `${number}:${number}`;

export const toPx = (x: number, y: number) => ({ x: x * UNIT_W, y: y * UNIT_H });
export const sizePx = (w: number, h: number) => ({ width: w * UNIT_W, height: h * UNIT_H });

export function worldRectFromViewport(vp: { x: number; y: number; zoom: number }, widthPx: number, heightPx: number): Rect {
  const x0 = (0 - vp.x) / vp.zoom / UNIT_W;
  const y0 = (0 - vp.y) / vp.zoom / UNIT_H;
  const x1 = (widthPx - vp.x) / vp.zoom / UNIT_W;
  const y1 = (heightPx - vp.y) / vp.zoom / UNIT_H;
  return { x0, y0, x1, y1 };
}

export function cellsForRect(rect: Rect, pad = 1): CellKey[] {
  const cx0 = Math.floor(rect.x0 / CELL) - pad, cy0 = Math.floor(rect.y0 / CELL) - pad;
  const cx1 = Math.ceil(rect.x1 / CELL) - 1 + pad, cy1 = Math.ceil(rect.y1 / CELL) - 1 + pad;
  const out: CellKey[] = [];
  for (let cx = cx0; cx <= cx1; cx++) for (let cy = cy0; cy <= cy1; cy++) out.push(`${cx}:${cy}`);
  return out;
}

export const parseCell = (key: CellKey) => key.split(":").map(Number) as [number, number];

export function cellRect(cells: CellKey[]): Rect {
  if (cells.length === 0) return { x0: 0, y0: 0, x1: 0, y1: 0 };
  const xs = cells.map((c) => parseCell(c)[0]), ys = cells.map((c) => parseCell(c)[1]);
  return { x0: Math.min(...xs) * CELL, y0: Math.min(...ys) * CELL,
           x1: (Math.max(...xs) + 1) * CELL, y1: (Math.max(...ys) + 1) * CELL };
}

/**
 * The server rejects a tiles rect wider or taller than 64 units, which is
 * exactly 8 cells per axis. A viewport padded by one cell exceeds that at low
 * zoom on a wide display, so a request has to be split.
 */
export const MAX_RECT_CELLS = 8;

/**
 * The subset of `cells` nearest `centre` whose bounding box still fits inside
 * the server's rect cap. Callers fetch this batch and ask again for whatever
 * is left, so the viewport centre loads first.
 */
export function boundedBatch(cells: CellKey[], centre: CellKey, max = MAX_RECT_CELLS): CellKey[] {
  const sorted = [...cells].sort((a, b) => cellDistance(a, centre) - cellDistance(b, centre));
  const out: CellKey[] = [];
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const c of sorted) {
    const [x, y] = parseCell(c);
    const nx0 = Math.min(x0, x), nx1 = Math.max(x1, x);
    const ny0 = Math.min(y0, y), ny1 = Math.max(y1, y);
    if (nx1 - nx0 + 1 > max || ny1 - ny0 + 1 > max) continue;
    out.push(c);
    x0 = nx0; x1 = nx1; y0 = ny0; y1 = ny1;
  }
  return out;
}

/** The cell holding the centre of `rect`. */
export function centreCell(rect: Rect): CellKey {
  const cx = Math.floor((rect.x0 + rect.x1) / 2 / CELL);
  const cy = Math.floor((rect.y0 + rect.y1) / 2 / CELL);
  return `${cx}:${cy}`;
}

export function cellDistance(a: CellKey, b: CellKey): number {
  const [ax, ay] = parseCell(a), [bx, by] = parseCell(b);
  return Math.max(Math.abs(ax - bx), Math.abs(ay - by));
}

export function maxDepthForZoom(zoom: number): number | null {
  if (zoom < 0.35) return 0;
  if (zoom < 0.6) return 1;
  return null;
}
