/** "3m ago" / "5h ago" — the same shape the CLI's activity table prints. */
export function relativeAge(seconds: number, now = Date.now() / 1000): string {
  const delta = Math.max(0, now - seconds);
  if (delta < 60) return `${Math.floor(delta)}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

export function absoluteTime(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString();
}
