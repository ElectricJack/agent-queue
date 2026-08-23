import { useEffect, useMemo, useState } from "react";
import { ReactFlow, ReactFlowProvider, Background, Controls } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { ArrowPathIcon, DocumentTextIcon } from "@heroicons/react/24/outline";
import type { PaneViewProps } from "../types";
import type { ProposalPreviewArgs } from "./manifest";
import { useDiscardProposal, useProposal, useProposalGate, useResolveGate } from "./hooks";
import { layoutProposalGraph } from "./graph";
import ProposalTaskNode from "./nodes";
import { useShellPaneStore } from "../store";

const nodeTypes = { proposalTask: ProposalTaskNode };

type SortKey = "title" | "priority";

function statusPillClass(status: string): string {
  switch (status) {
    case "ready":
      return "bg-amber-500/20 text-amber-300";
    case "committed":
      return "bg-emerald-500/20 text-emerald-300";
    case "discarded":
      return "bg-gray-700/40 text-gray-400 line-through";
    default:
      return "bg-gray-700/40 text-gray-300";
  }
}

function relativeAge(createdAt: number | null | undefined): string | null {
  if (createdAt == null) return null;
  const seconds = Date.now() / 1000 - createdAt;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function sourceSpecPath(source: string | undefined): string | undefined {
  if (!source) return undefined;
  const stripped = source.startsWith("spec:") ? source.slice("spec:".length) : source;
  if (!stripped || !stripped.endsWith(".md")) return undefined;
  return stripped;
}

export default function ProposalPreviewPane({
  args,
  close,
  setToolbar,
  setShortcuts,
}: PaneViewProps<ProposalPreviewArgs>) {
  const proposalQ = useProposal(args.proposalId);
  const data = proposalQ.data;
  const gateQ = useProposalGate(data?.project_id, args.proposalId);
  const resolveGate = useResolveGate();
  const discard = useDiscardProposal(args.proposalId);
  const pane = useShellPaneStore();

  const [sortBy, setSortBy] = useState<SortKey>("title");
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [mutationError, setMutationError] = useState<string | null>(null);

  const specPath = sourceSpecPath(data?.source);

  const handleRefresh = () => {
    proposalQ.refetch();
  };

  const handleViewSource = () => {
    if (!specPath) return;
    pane.open("spec-doc-reader", { url: specPath });
  };

  async function handleApprove() {
    if (!gateQ.gate) return;
    setMutationError(null);
    try {
      await resolveGate.mutateAsync({
        gate_id: gateQ.gate.id,
        resolved_by: "dashboard",
        resolution: "approved",
      });
      close();
    } catch (e) {
      setMutationError(`Approve failed: ${(e as Error).message}`);
    }
  }

  async function handleDiscard() {
    setMutationError(null);
    try {
      await discard.mutateAsync();
      close();
    } catch (e) {
      setMutationError(`Discard failed: ${(e as Error).message}`);
      setConfirmDiscard(false);
    }
  }

  // Toolbar + shortcuts register unconditionally, before any early return
  // (plugin-interface contract §5.1/§5.2).
  useEffect(() => {
    setToolbar([
      {
        id: "refresh",
        label: "Refresh",
        icon: ArrowPathIcon,
        onClick: handleRefresh,
      },
      {
        id: "view-source",
        label: "View spec source",
        icon: DocumentTextIcon,
        disabled: !specPath,
        onClick: handleViewSource,
      },
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setToolbar, specPath]);

  useEffect(() => {
    const status = data?.status;
    const bindings = [
      { key: "r", label: "Refresh", onFire: handleRefresh },
      { key: "s", label: "View spec source", onFire: handleViewSource },
      ...(status === "ready"
        ? [
            { key: "a", label: "Approve", onFire: handleApprove },
            {
              key: "d",
              label: "Discard (confirm)",
              onFire: () => setConfirmDiscard(true),
            },
          ]
        : []),
    ];
    setShortcuts(bindings);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setShortcuts, data?.status, specPath, gateQ.gate]);

  const { nodes, edges } = useMemo(
    () => layoutProposalGraph(data?.tasks ?? [], data?.edges ?? []),
    [data?.tasks, data?.edges],
  );

  const sortedTasks = useMemo(() => {
    const tasks = [...(data?.tasks ?? [])];
    if (sortBy === "priority") {
      tasks.sort((a, b) => (b.priority ?? 100) - (a.priority ?? 100));
    } else {
      tasks.sort((a, b) => a.title.localeCompare(b.title));
    }
    return tasks;
  }, [data?.tasks, sortBy]);

  if (proposalQ.isPending) {
    return (
      <div className="flex h-full flex-col gap-3 p-3" data-testid="proposal-preview-pane">
        <div className="h-4 w-40 animate-pulse rounded bg-gray-800" />
        <div className="h-40 animate-pulse rounded bg-gray-900" />
        <div className="h-4 w-full animate-pulse rounded bg-gray-800" />
        <div className="h-4 w-full animate-pulse rounded bg-gray-800" />
        <div className="h-4 w-full animate-pulse rounded bg-gray-800" />
      </div>
    );
  }

  if (proposalQ.isError || !data) {
    return (
      <div
        className="flex h-full flex-col items-center justify-center gap-3 p-4 text-sm text-gray-300"
        data-testid="proposal-preview-pane"
      >
        <p>Proposal not found (or failed to load)</p>
        <p className="font-mono text-xs opacity-70">{args.proposalId}</p>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => proposalQ.refetch()}
            className="rounded bg-gray-800 px-3 py-1 text-xs hover:bg-gray-700"
          >
            Retry
          </button>
          <button
            type="button"
            onClick={close}
            className="rounded bg-gray-800 px-3 py-1 text-xs hover:bg-gray-700"
          >
            Close
          </button>
        </div>
      </div>
    );
  }

  const age = relativeAge(gateQ.gate?.created_at ?? null);
  const showActions = data.status === "ready";

  return (
    <div className="flex h-full flex-col gap-3 p-3 text-sm text-gray-200" data-testid="proposal-preview-pane">
      <div>
        <div className="flex items-center gap-2">
          <span
            className="truncate font-mono text-xs opacity-80"
            title={data.proposal_id}
          >
            {data.proposal_id}
          </span>
          <span
            className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${statusPillClass(data.status)}`}
          >
            {data.status}
          </span>
        </div>
        {data.source && (
          <p className="mt-1 truncate text-xs text-gray-400">
            from spec: {data.source.startsWith("spec:") ? data.source.slice(5) : data.source}
          </p>
        )}
        {age && <p className="mt-1 text-xs text-gray-500">proposed {age}</p>}
      </div>

      <div
        className="h-[280px] rounded border border-gray-800 bg-gray-950"
        data-testid="proposal-graph"
      >
        <ReactFlowProvider>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            fitView
            nodesDraggable={false}
            nodesConnectable={false}
            elementsSelectable
            onNodeClick={(_, n) => setSelectedNode(n.id)}
          >
            <Background gap={20} color="#1f2937" />
            <Controls className="!bg-gray-900 !text-gray-200" showInteractive={false} />
          </ReactFlow>
        </ReactFlowProvider>
      </div>

      <div className="flex-1 overflow-auto">
        <div className="mb-1 flex items-center justify-between">
          <span className="text-xs font-medium text-gray-300">
            Proposed tasks ({data.tasks.length})
          </span>
          <div className="flex gap-1 text-[10px]">
            <button
              type="button"
              onClick={() => setSortBy("title")}
              className={`rounded px-1.5 py-0.5 ${sortBy === "title" ? "bg-indigo-500/30 text-indigo-200" : "bg-gray-800 text-gray-400"}`}
            >
              title
            </button>
            <button
              type="button"
              onClick={() => setSortBy("priority")}
              className={`rounded px-1.5 py-0.5 ${sortBy === "priority" ? "bg-indigo-500/30 text-indigo-200" : "bg-gray-800 text-gray-400"}`}
            >
              priority
            </button>
          </div>
        </div>
        <ul
          className="divide-y divide-gray-800 rounded border border-gray-800"
          data-testid="proposal-task-list"
        >
          {sortedTasks.map((t) => (
            <li
              key={t.tempId}
              onClick={() => setSelectedNode(t.tempId)}
              className={`flex cursor-pointer items-center gap-2 px-2 py-1 text-xs ${
                selectedNode === t.tempId ? "bg-indigo-950/60" : "hover:bg-gray-900"
              }`}
              title={t.description}
            >
              <span className="flex-1 truncate">{t.title}</span>
              <span className="text-gray-500">P{t.priority ?? 100}</span>
            </li>
          ))}
        </ul>
      </div>

      {mutationError && (
        <div className="rounded bg-red-950/60 px-2 py-1 text-xs text-red-300">
          {mutationError}
        </div>
      )}

      {data.status === "committed" && (
        <p className="text-xs text-emerald-400">
          Committed — {data.tasks.length} tasks created.
        </p>
      )}
      {data.status === "discarded" && (
        <p className="text-xs text-gray-500">Discarded — no tasks were created.</p>
      )}

      {showActions && (
        <div className="flex items-center justify-between gap-3 border-t border-gray-800 pt-3">
          {confirmDiscard ? (
            <div className="flex items-center gap-2 text-xs">
              <span>Really discard?</span>
              <button
                type="button"
                onClick={handleDiscard}
                disabled={discard.isPending}
                className="rounded bg-red-600/80 px-2 py-1 text-white hover:bg-red-600"
              >
                Yes
              </button>
              <button
                type="button"
                onClick={() => setConfirmDiscard(false)}
                className="rounded bg-gray-800 px-2 py-1 hover:bg-gray-700"
              >
                No
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmDiscard(true)}
              disabled={discard.isPending}
              className="rounded border border-red-500/40 px-3 py-1 text-xs text-red-300 hover:bg-red-950/40"
            >
              {discard.isPending ? "Discarding…" : "Discard"}
            </button>
          )}
          {!gateQ.gate && (
            <span className="text-[10px] text-gray-500">Waiting for approval gate to appear…</span>
          )}
          <button
            type="button"
            onClick={handleApprove}
            disabled={!gateQ.gate || resolveGate.isPending}
            className="rounded bg-indigo-600 px-3 py-1 text-xs text-white hover:bg-indigo-500 disabled:opacity-40"
          >
            {resolveGate.isPending ? "Approving…" : "Approve"}
          </button>
        </div>
      )}
    </div>
  );
}
