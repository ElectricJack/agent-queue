export const CONNECTION_RESTORED = "aq:connection-restored";

/** One viewer's retry timer. No input or output is retained here. */
export function reconnectLoop(connect: () => void, waiting: (attempt: number) => void) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let stableTimer: ReturnType<typeof setTimeout> | undefined;
  let stopped = false;
  let pending = false;
  let attempt = 0;
  let visible = true;
  const available = () => visible && document.visibilityState !== "hidden" && navigator.onLine !== false;
  const cancel = () => { clearTimeout(timer); timer = undefined; };
  const schedule = (delay: number) => {
    cancel();
    if (stopped || !pending || !available()) return;
    timer = setTimeout(() => {
      timer = undefined;
      if (stopped || !pending || !available()) return;
      pending = false;
      connect();
    }, delay);
  };
  const recover = () => { if (pending) schedule(0); return pending; };
  const resume = () => schedule(Math.min(15_000, 500 * 2 ** Math.min(attempt - 1, 5)) * (0.8 + Math.random() * 0.2));
  const visibility = () => { if (available()) recover(); else cancel(); };
  document.addEventListener("visibilitychange", visibility);
  window.addEventListener("online", recover);
  window.addEventListener("offline", visibility);
  window.addEventListener(CONNECTION_RESTORED, recover);
  return {
    start() { pending = true; schedule(0); },
    retry(diagnosing = false) {
      if (stopped) return;
      clearTimeout(stableTimer);
      pending = true;
      attempt += 1;
      waiting(attempt);
      // Jitter stays below the cap, including after arbitrarily many failures.
      if (!diagnosing) resume();
    },
    connected() {
      pending = false;
      cancel();
      clearTimeout(stableTimer);
      stableTimer = setTimeout(() => { attempt = 0; }, 30_000);
    },
    recover,
    resume,
    get attempt() { return attempt; },
    available,
    setVisible(value: boolean) { visible = value; visibility(); },
    stop() {
      stopped = true;
      pending = false;
      cancel();
      clearTimeout(stableTimer);
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("online", recover);
      window.removeEventListener("offline", visibility);
      window.removeEventListener(CONNECTION_RESTORED, recover);
    },
  };
}
