/**
 * The dashboard's only caller of `crypto.randomUUID`.
 *
 * Browsers expose `crypto.randomUUID` only in secure contexts (HTTPS or
 * localhost), so a bare call throws `crypto.randomUUID is not a function` when
 * the dashboard is served over plain HTTP from a LAN address. `getRandomValues`
 * has no such restriction, so it backs an RFC 4122 version 4 fallback.
 * `tests/test_dashboard_secure_context.py` fails CI if any other module calls
 * `randomUUID` directly.
 */
export function randomUUID(): string {
  const generator = typeof globalThis === "undefined" ? undefined : globalThis.crypto;
  if (typeof generator?.randomUUID === "function") return generator.randomUUID();

  const bytes = new Uint8Array(16);
  if (typeof generator?.getRandomValues === "function") generator.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40;
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
