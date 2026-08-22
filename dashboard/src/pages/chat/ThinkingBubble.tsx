import { useEffect, useState } from "react";
import type { ThinkingState } from "./useChatTranscript";

/**
 * Live "supervisor is thinking" indicator. Rendered inline in the transcript
 * from the moment the user sends a message until the supervisor's reply lands
 * (a message.sent WS event with to_kind=user, or a hydration re-fetch that
 * surfaces a fresh non-user row). Activity chips are collected in real time
 * from side-effect events the supervisor triggers while working — task
 * starts/completes, gates opening, playbook runs, streaming task output.
 */
export default function ThinkingBubble({ thinking }: { thinking: ThinkingState }) {
  const [elapsed, setElapsed] = useState(() => Date.now() / 1000 - thinking.since);

  useEffect(() => {
    const id = window.setInterval(() => {
      setElapsed(Date.now() / 1000 - thinking.since);
    }, 500);
    return () => window.clearInterval(id);
  }, [thinking.since]);

  const secs = Math.max(0, Math.floor(elapsed));
  const timeLabel =
    secs < 60
      ? `${secs}s`
      : `${Math.floor(secs / 60)}m ${String(secs % 60).padStart(2, "0")}s`;

  return (
    <div className="flex justify-start" aria-live="polite">
      <div className="w-full max-w-[85%] rounded-lg border border-indigo-500/30 bg-indigo-500/5 px-3 py-2 text-sm">
        <div className="mb-1 flex items-center gap-2 text-xs">
          <span className="relative inline-flex h-2.5 w-2.5">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-indigo-400 opacity-60" />
            <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-indigo-400" />
          </span>
          <span className="font-medium text-indigo-300">Supervisor is thinking…</span>
          <span className="ml-auto font-mono text-gray-500">{timeLabel}</span>
        </div>

        {thinking.activities.length === 0 ? (
          <div className="flex items-center gap-1 text-xs text-gray-500">
            <span className="inline-block h-1 w-1 animate-pulse rounded-full bg-gray-500" />
            <span className="inline-block h-1 w-1 animate-pulse rounded-full bg-gray-500 [animation-delay:150ms]" />
            <span className="inline-block h-1 w-1 animate-pulse rounded-full bg-gray-500 [animation-delay:300ms]" />
            <span className="ml-2">waiting for output…</span>
          </div>
        ) : (
          <ul className="space-y-0.5 text-xs">
            {thinking.activities.slice(-8).map((a, i) => (
              <li
                key={`${a.ts}-${i}`}
                className="flex items-baseline gap-2 text-gray-300"
              >
                <span className="text-[10px] text-gray-500">
                  +{Math.max(0, Math.floor(a.ts - thinking.since))}s
                </span>
                <span className="truncate">{a.label}</span>
              </li>
            ))}
            {thinking.activities.length > 8 && (
              <li className="text-[10px] text-gray-500">
                …{thinking.activities.length - 8} earlier
              </li>
            )}
          </ul>
        )}
      </div>
    </div>
  );
}
