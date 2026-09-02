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
  const xs = cells.map((c) => parseCell(c)[0]), ys = cells.map((c) => parseCell(c)[1]);
  return { x0: Math.min(...xs) * CELL, y0: Math.min(...ys) * CELL,
           x1: (Math.max(...xs) + 1) * CELL, y1: (Math.max(...ys) + 1) * CELL };
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
