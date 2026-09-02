/** Presentation-only density. Server world coordinates and ordinals stay stable. */
export const DENSITY_STORAGE_KEY = "aq.command-center.graph-density";
export const DEFAULT_DENSITY = "comfortable" as const;

export type LayoutDensity = "compact" | "comfortable" | "spacious";

export const DENSITY_SCALE: Record<LayoutDensity, number> = {
  compact: 0.86,
  comfortable: 1,
  spacious: 1.12,
};

export function storedDensity(): LayoutDensity {
  if (typeof window === "undefined") return DEFAULT_DENSITY;
  const value = window.localStorage.getItem(DENSITY_STORAGE_KEY);
  return value === "compact" || value === "spacious" || value === "comfortable"
    ? value
    : DEFAULT_DENSITY;
}
