export type Platform = { modifier: "cmd" | "ctrl" };

/** Detects the modifier convention from the UA. Mac → cmd (meta), else ctrl. */
export function detectPlatform(): Platform {
  const ua = typeof navigator !== "undefined" ? navigator.userAgent : "";
  const isMac = /Mac|iPhone|iPad|iPod/i.test(ua);
  return { modifier: isMac ? "cmd" : "ctrl" };
}

/** Normalizes `$mod` in a shortcut spec to the current-platform modifier. */
export function expandMod(key: string): string {
  const p = detectPlatform();
  return key.replace(/\$mod/g, p.modifier === "cmd" ? "meta" : "ctrl");
}
