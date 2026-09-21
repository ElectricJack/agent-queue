export interface ProgressBarProps {
  done: number;
  total: number;
  running?: number;
  blocked?: number;
  className?: string;
}

/**
 * A thin segmented progress bar shared by container nodes, collapsed
 * containers/stubs and task cards with subtasks. `done`/`running`/`blocked`
 * are counts, not percentages, and are read against `total` — an odd state
 * (a task counted as both running and blocked) can push their sum past
 * `total`, so each segment is clamped to what is left of the bar rather than
 * drawn at its raw width. Renders nothing when there is nothing to show a
 * fraction of.
 */
export function ProgressBar({ done, total, running = 0, blocked = 0, className = "" }: ProgressBarProps) {
  if (total <= 0) return null;
  const doneW = Math.max(0, Math.min(done, total));
  const runningW = Math.max(0, Math.min(running, total - doneW));
  const blockedW = Math.max(0, Math.min(blocked, total - doneW - runningW));
  const pct = (n: number) => (n / total) * 100;
  return (
    <span
      role="progressbar"
      aria-label={`${done} of ${total} done`}
      aria-valuemin={0}
      aria-valuemax={total}
      aria-valuenow={doneW}
      className={`flex h-1 w-full overflow-hidden rounded bg-white/10 ${className}`}
    >
      <span aria-hidden data-segment="done" className="block h-full bg-emerald-400" style={{ width: `${pct(doneW)}%` }} />
      <span aria-hidden data-segment="running" className="block h-full bg-indigo-400" style={{ width: `${pct(runningW)}%` }} />
      <span aria-hidden data-segment="blocked" className="block h-full bg-amber-400" style={{ width: `${pct(blockedW)}%` }} />
    </span>
  );
}
