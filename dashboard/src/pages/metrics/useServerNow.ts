import { useEffect, useState } from "react";

/**
 * The server's clock, advanced locally between polls.
 *
 * A countdown to a provider's expected recovery must read against the clock
 * the daemon judged it by — a browser minutes out of sync would otherwise
 * show "back in 5m" for a provider the daemon expects in 10.  So the reading
 * starts at the response's ``now`` and only the time elapsed since it arrived
 * comes from the browser.  Without a server reading it falls back to the
 * browser clock.  ``tickMs`` is coarse on purpose: the countdown prints
 * minutes.
 */
export function useServerNow(
  serverNow: number | undefined,
  receivedAtMs: number,
  tickMs = 15_000,
): number {
  const [tick, setTick] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setTick(Date.now()), tickMs);
    return () => window.clearInterval(id);
  }, [tickMs]);
  if (serverNow == null || !Number.isFinite(serverNow)) return tick / 1000;
  return serverNow + Math.max(0, tick - receivedAtMs) / 1000;
}
