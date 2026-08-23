import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  ArrowPathIcon,
  PlayIcon,
  XCircleIcon,
  ArrowTopRightOnSquareIcon,
} from "@heroicons/react/24/outline";
import StatusBadge from "../../components/StatusBadge";
import Modal from "../../components/Modal";
import {
  useInspectPlaybookRun,
  useResumePlaybookRun,
  useCancelPlaybookRun,
} from "../../api/hooks";
import { useEventStream } from "../../ws/useEventStream";
import type { PaneViewProps } from "../types";
import type { PlaybookRunInspectorArgs } from "./manifest";

interface NodeTraceEntry {
  node_id: string;
  started_at: number;
  completed_at: number | null;
  status: string;
  transition_to?: string;
  transition_method?: string;
  tokens_used?: number;
  command?: string;
  args_summary?: string;
  output?: string;
  error?: string;
  duration_seconds?: number;
}

const TERMINAL_STATUSES = new Set(["completed", "failed", "timed_out", "cancelled"]);

const RELEVANT_RUN_EVENTS = new Set([
  "notify.playbook_run_paused",
  "notify.playbook_run_resumed",
  "notify.playbook_run_completed",
  "notify.playbook_run_failed",
  "notify.playbook_run_timed_out",
  "notify.playbook_run_cancelled",
  "notify.playbook_run_node_started",
  "notify.playbook_run_node_completed",
]);

function isNotFoundError(error: unknown): boolean {
  const msg = error instanceof Error ? error.message : String(error);
  return /not found/i.test(msg);
}

function HitlBanner({ runId }: { runId: string }) {
  const resume = useResumePlaybookRun();
  const [text, setText] = useState("");

  return (
    <div className="border-t border-gray-800 bg-purple-500/5 p-3">
      <div className="mb-2 text-xs font-medium uppercase text-purple-300">Waiting on you</div>
      <div className="flex items-center gap-2">
        <button
          onClick={() => resume.mutate({ run_id: runId, human_input: "approve" })}
          disabled={resume.isPending}
          className="rounded-md bg-green-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-green-500 disabled:opacity-50"
        >
          Approve
        </button>
        <button
          onClick={() => resume.mutate({ run_id: runId, human_input: "reject" })}
          disabled={resume.isPending}
          className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
        >
          Reject
        </button>
      </div>
      <div className="mt-2 flex items-center gap-2">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="or reply…"
          className="flex-1 rounded-md border border-gray-700 bg-gray-950 px-2 py-1 text-sm text-gray-200 focus:border-purple-500 focus:outline-none"
        />
        <button
          onClick={() => {
            if (!text.trim()) return;
            resume.mutate({ run_id: runId, human_input: text });
            setText("");
          }}
          disabled={resume.isPending || !text.trim()}
          className="rounded-md bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700 disabled:opacity-50"
        >
          Send
        </button>
      </div>
      {resume.isError && (
        <div className="mt-2 text-xs text-red-400">
          {resume.error instanceof Error ? resume.error.message : "Resume failed."}
        </div>
      )}
    </div>
  );
}

export default function PlaybookRunInspectorPane({
  args,
  setToolbar,
  setShortcuts,
}: PaneViewProps<PlaybookRunInspectorArgs>) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data: run, isLoading, isError, error, refetch } = useInspectPlaybookRun(args.runId);
  const trace = (run?.node_trace ?? []) as unknown as NodeTraceEntry[];
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const [cancelModalOpen, setCancelModalOpen] = useState(false);

  const effectiveIndex = useMemo(() => {
    if (selectedIndex !== null && selectedIndex >= 0 && selectedIndex < trace.length) {
      return selectedIndex;
    }
    return trace.length > 0 ? trace.length - 1 : null;
  }, [selectedIndex, trace.length]);

  const selectedEntry = effectiveIndex !== null ? trace[effectiveIndex] : null;

  // Must stay inside effects: `setToolbar`/`setShortcuts` are ShellPaneHost
  // useState setters, so publishing during render re-renders the parent on
  // every pass and loops forever. Effects still run before any early return,
  // which is what the plugin-interface contract actually requires.
  useEffect(() => {
    setToolbar([
      { id: "refresh", label: "Refresh", icon: ArrowPathIcon, onClick: () => refetch() },
      ...(run?.status === "paused"
        ? [
            {
              id: "resume",
              label: "Resume",
              icon: PlayIcon,
              onClick: () => {
                const el = document.querySelector<HTMLInputElement>(
                  'input[placeholder="or reply…"]',
                );
                el?.focus();
              },
            },
          ]
        : []),
      {
        id: "cancel",
        label: "Cancel",
        icon: XCircleIcon,
        onClick: () => setCancelModalOpen(true),
        disabled: !run || TERMINAL_STATUSES.has(run.status),
      },
      {
        id: "open-playbook",
        label: "Open playbook page",
        icon: ArrowTopRightOnSquareIcon,
        onClick: () => run && navigate(`/playbooks/${encodeURIComponent(run.playbook_id)}`),
        disabled: !run,
      },
    ]);
    return () => setToolbar([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.status, run?.playbook_id]);

  useEffect(() => {
    setShortcuts([
      {
        key: "ArrowUp",
        label: "Previous node",
        onFire: () => setSelectedIndex((i) => Math.max(0, (i ?? trace.length - 1) - 1)),
      },
      {
        key: "ArrowDown",
        label: "Next node",
        onFire: () => setSelectedIndex((i) => Math.min(trace.length - 1, (i ?? 0) + 1)),
      },
      { key: "Enter", label: "Expand node detail", onFire: () => {} },
      {
        key: "r",
        label: "Resume run",
        onFire: () => {
          if (run?.status !== "paused") return;
          document.querySelector<HTMLInputElement>('input[placeholder="or reply…"]')?.focus();
        },
      },
      {
        key: "x",
        label: "Cancel run",
        onFire: () => {
          if (run && !TERMINAL_STATUSES.has(run.status)) setCancelModalOpen(true);
        },
      },
    ]);
    return () => setShortcuts([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.status, trace.length]);

  useEventStream({
    onEvent: (event) => {
      const runId = (event as { run_id?: string }).run_id;
      if (runId !== args.runId) return;
      if (RELEVANT_RUN_EVENTS.has(event.event_type)) {
        queryClient.invalidateQueries({ queryKey: ["playbook-run", args.runId] });
      }
    },
  });

  let body: React.ReactNode;
  if (isLoading) {
    body = (
      <div data-testid="run-loading" className="space-y-2 p-4">
        <div className="h-4 w-1/2 animate-pulse rounded bg-gray-800" />
        <div className="h-4 w-full animate-pulse rounded bg-gray-800" />
        <div className="h-4 w-full animate-pulse rounded bg-gray-800" />
      </div>
    );
  } else if (isError) {
    body = isNotFoundError(error) ? (
      <div className="flex h-full items-center justify-center p-4 text-sm text-gray-400">
        Run {args.runId} not found.
      </div>
    ) : (
      <div className="space-y-3 p-4">
        <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
          {error instanceof Error ? error.message : "Failed to load playbook run."}
        </div>
        <button
          onClick={() => refetch()}
          className="rounded-md bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
        >
          Retry
        </button>
      </div>
    );
  } else if (!run) {
    body = null;
  } else {
    body = (
      <div className="flex h-full flex-col">
        <div className="flex items-center justify-between border-b border-gray-800 px-4 py-2 text-sm">
          <div className="flex items-center gap-2 font-mono text-gray-300">
            <span>{run.playbook_id}</span>
            <span className="text-gray-600">·</span>
            <span>v{run.playbook_version}</span>
            <span className="text-gray-600">·</span>
            <span>run {run.run_id.slice(0, 8)}…</span>
          </div>
          <StatusBadge status={run.status} />
        </div>

        <div className="max-h-[40%] overflow-y-auto border-b border-gray-800">
          {trace.map((entry, idx) => (
            <button
              key={`${entry.node_id}-${idx}`}
              onClick={() => setSelectedIndex(idx)}
              className={`flex w-full items-center justify-between px-4 py-2 text-left text-sm hover:bg-gray-800/50 ${
                idx === effectiveIndex ? "bg-gray-800/70" : ""
              }`}
            >
              <span className="flex items-center gap-2 font-mono text-gray-200">
                {entry.node_id === run.current_node && run.status === "running" && (
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-green-400" />
                )}
                {entry.node_id}
              </span>
              <span className="flex items-center gap-3">
                <span className="text-xs text-gray-500">
                  {entry.duration_seconds != null ? `${entry.duration_seconds}s` : "—"}
                </span>
                <StatusBadge status={entry.status} />
              </span>
            </button>
          ))}
        </div>

        <div data-testid="node-detail" className="flex-1 overflow-y-auto p-4 text-sm">
          {selectedEntry ? (
            <div className="space-y-3">
              <div className="font-mono text-gray-300">
                node: {selectedEntry.node_id} · status: {selectedEntry.status}
                {selectedEntry.transition_to && (
                  <> · → {selectedEntry.transition_to} via {selectedEntry.transition_method}</>
                )}
              </div>
              {selectedEntry.output != null ? (
                <pre className="max-h-64 overflow-auto rounded-md bg-gray-950 p-3 text-xs text-gray-300">
                  {selectedEntry.output}
                </pre>
              ) : (
                <div className="space-y-2">
                  <p className="text-xs text-gray-500">
                    Node-level command/output detail isn't available for this run yet — see
                    conversation history below.
                  </p>
                  <div className="space-y-2 rounded-md bg-gray-950 p-3">
                    {(run.conversation_history ?? []).map((msg, i) => (
                      <div key={i} className="text-xs text-gray-400">
                        <span className="font-mono text-gray-600">
                          {String((msg as Record<string, unknown>).role)}:{" "}
                        </span>
                        {String((msg as Record<string, unknown>).content)}
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {selectedEntry.error && (
                <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-300">
                  {selectedEntry.error}
                </div>
              )}
            </div>
          ) : (
            <p className="text-gray-500">No nodes visited yet.</p>
          )}
        </div>

        {run.status === "paused" && !run.waiting_for_event && <HitlBanner runId={run.run_id} />}
        {run.status === "paused" && run.waiting_for_event && (
          <div className="border-t border-gray-800 p-3 text-xs text-gray-400">
            Waiting for event: {run.waiting_for_event}
          </div>
        )}
      </div>
    );
  }

  return (
    <>
      {body}
      {run && (
        <CancelPlaybookRunModal
          open={cancelModalOpen}
          onClose={() => setCancelModalOpen(false)}
          runId={run.run_id}
        />
      )}
    </>
  );
}

function CancelPlaybookRunModal({
  open,
  onClose,
  runId,
}: {
  open: boolean;
  onClose: () => void;
  runId: string;
}) {
  const cancel = useCancelPlaybookRun();
  const [fatal, setFatal] = useState<string | null>(null);

  const onConfirm = async () => {
    setFatal(null);
    try {
      await cancel.mutateAsync({ run_id: runId });
      onClose();
    } catch (err) {
      setFatal(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Cancel run">
      <div className="space-y-4">
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">
          Cancel this run? It will stop advancing through the playbook graph.
        </div>
        {fatal && (
          <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
            {fatal}
          </div>
        )}
        <div className="flex items-center justify-end gap-2 border-t border-gray-800 pt-3">
          <button
            onClick={onClose}
            className="rounded-md bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
          >
            Keep running
          </button>
          <button
            onClick={onConfirm}
            disabled={cancel.isPending}
            className="rounded-md bg-red-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:cursor-not-allowed disabled:bg-gray-700"
          >
            {cancel.isPending ? "Cancelling…" : "Cancel run"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
