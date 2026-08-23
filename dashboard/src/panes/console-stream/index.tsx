import { useEffect, useMemo, useRef, useState } from "react";
import type { PaneViewProps } from "../types";
import { useConsoleStream, type ConsoleLine, type ConsoleStreamStatus } from "./hooks";
import type { ConsoleStreamArgs } from "./manifest";
import { ansiToSpans, stripAnsi } from "./ansi";

const ROW_HEIGHT = 20;
const OVERSCAN = 10;

function useElapsed(startedAt: number | null, endedAt: number | null): string {
  const [, forceTick] = useState(0);
  useEffect(() => {
    if (endedAt !== null || startedAt === null) return;
    const id = window.setInterval(() => forceTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [startedAt, endedAt]);
  if (startedAt === null) return "";
  const end = endedAt ?? Date.now() / 1000;
  const secs = Math.max(0, Math.round(end - startedAt));
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return m > 0 ? `${m}m ${s.toString().padStart(2, "0")}s` : `${s}s`;
}

/**
 * The dashboard's own authenticated session identity. Stubbed via
 * localStorage until a shared shell-level identity hook exists — the shell
 * spec's identity plumbing is out of scope for this plan (Deviation #1).
 */
function useOwnSessionId(): string | null {
  try {
    return window.localStorage.getItem("aq:session:id");
  } catch {
    return null;
  }
}

export default function ConsoleStreamPane({
  args,
  setToolbar,
  setShortcuts,
}: PaneViewProps<ConsoleStreamArgs>) {
  const ownSessionId = useOwnSessionId();
  const scopeMismatch = !!args.sessionId && !!ownSessionId && args.sessionId !== ownSessionId;

  const stream = useConsoleStream(scopeMismatch ? null : args.streamId);
  const [followTail, setFollowTail] = useState(true);
  const [killConfirming, setKillConfirming] = useState(false);
  const [copied, setCopied] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportHeight, setViewportHeight] = useState(0);

  const elapsed = useElapsed(stream.startedAt, stream.endedAt);

  const doKill = async () => {
    setKillConfirming(false);
    await fetch(`/api/streams/${encodeURIComponent(args.streamId)}/kill`, { method: "POST" });
  };

  const doCopy = async () => {
    const text = stream.lines.map((l) => stripAnsi(l.text)).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard permission denied — silently no-op; the toolbar label
      // just won't flip to "Copied".
    }
  };

  useEffect(() => {
    setToolbar([
      {
        id: "pause-tail",
        label: followTail ? "Pause tail" : "Resume tail",
        onClick: () => setFollowTail((v) => !v),
      },
      { id: "copy-output", label: copied ? "Copied" : "Copy output", onClick: () => void doCopy() },
      ...(stream.status === "running"
        ? [{ id: "kill", label: "Kill", onClick: () => setKillConfirming(true) }]
        : []),
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [followTail, copied, stream.status, stream.lines.length]);

  useEffect(() => {
    setShortcuts([
      { key: "space", label: "Toggle follow-tail", onFire: () => setFollowTail((v) => !v) },
      {
        key: "k",
        label: "Kill",
        onFire: () => {
          if (stream.status === "running") setKillConfirming(true);
        },
      },
      { key: "c", label: "Copy output", onFire: () => void doCopy() },
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stream.status, stream.lines.length]);

  useEffect(() => {
    if (stream.status === "exited" || stream.status === "killed") setFollowTail(false);
  }, [stream.status]);

  useEffect(() => {
    if (!followTail || !scrollRef.current) return;
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [stream.lines.length, followTail]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    setScrollTop(el.scrollTop);
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < ROW_HEIGHT;
    if (!atBottom && followTail) setFollowTail(false);
    if (atBottom && !followTail && stream.status === "running") setFollowTail(true);
  };

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    setViewportHeight(el.clientHeight);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setViewportHeight(el.clientHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const totalRows = stream.lines.length;
  const start = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const visibleCount = Math.ceil(viewportHeight / ROW_HEIGHT) + OVERSCAN * 2;
  const end = Math.min(totalRows, start + Math.max(visibleCount, 1));
  const visible = useMemo(() => stream.lines.slice(start, end), [stream.lines, start, end]);

  if (scopeMismatch) {
    return (
      <div role="status" className="p-4 text-sm text-neutral-400">
        You don&apos;t have access to this console output.
      </div>
    );
  }

  if (stream.status === "error" && stream.errorMessage) {
    return (
      <div role="status" className="p-4 text-sm text-amber-500">
        connection lost
        <button className="ml-2 underline" onClick={() => window.location.reload()}>
          Retry
        </button>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col bg-neutral-950 text-neutral-100 font-mono text-xs">
      <div className="flex items-center gap-2 border-b border-neutral-800 px-2 py-1">
        <StatusChip status={stream.status} exitCode={stream.exitCode} elapsed={elapsed} />
      </div>
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="relative flex-1 overflow-y-auto"
      >
        <div style={{ height: totalRows * ROW_HEIGHT, position: "relative" }}>
          {visible.map((line, i) => (
            <Row key={line.seq} line={line} top={(start + i) * ROW_HEIGHT} />
          ))}
        </div>
        {(stream.status === "exited" || stream.status === "killed") && (
          <ExitBanner status={stream.status} exitCode={stream.exitCode} elapsed={elapsed} />
        )}
      </div>
      {killConfirming && (
        <div className="flex items-center gap-2 border-t border-neutral-800 p-2" role="dialog" aria-label="Kill this process?">
          <span>Kill this process?</span>
          <button className="rounded bg-red-600 px-2 py-1" onClick={() => void doKill()}>
            Confirm
          </button>
          <button className="rounded bg-neutral-700 px-2 py-1" onClick={() => setKillConfirming(false)}>
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

function Row({ line, top }: { line: ConsoleLine; top: number }) {
  return (
    <div
      style={{ position: "absolute", top, left: 0, right: 0, height: ROW_HEIGHT, lineHeight: `${ROW_HEIGHT}px` }}
      className={line.stream === "stderr" ? "border-l-2 border-red-500 pl-1" : "pl-1"}
    >
      {ansiToSpans(line.text)}
    </div>
  );
}

function StatusChip({
  status, exitCode, elapsed,
}: { status: ConsoleStreamStatus; exitCode: number | null; elapsed: string }) {
  if (status === "connecting") return <span>connecting…</span>;
  if (status === "running") return <span>running {elapsed}</span>;
  if (status === "exited") {
    return <span>{exitCode === 0 ? "exited (0)" : `exited (${exitCode})`}</span>;
  }
  if (status === "killed") return <span>killed</span>;
  return <span>connection lost</span>;
}

function ExitBanner({
  status, exitCode, elapsed,
}: { status: ConsoleStreamStatus; exitCode: number | null; elapsed: string }) {
  const label =
    status === "killed" ? `killed after ${elapsed}` : `exited with code ${exitCode} after ${elapsed}`;
  return (
    <div className="border-t border-neutral-800 py-1 text-center text-neutral-400">
      —— {label} ——
    </div>
  );
}
