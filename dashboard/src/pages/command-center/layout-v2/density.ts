/** Presentation-only density. Server world coordinates and ordinals stay stable. */
export const DEFAULT_DENSITY = "comfortable" as const;

export type LayoutDensity = "compact" | "comfortable" | "spacious";

export const DENSITY_SCALE: Record<LayoutDensity, number> = {
  compact: 0.86,
  comfortable: 1,
  spacious: 1.12,
};
