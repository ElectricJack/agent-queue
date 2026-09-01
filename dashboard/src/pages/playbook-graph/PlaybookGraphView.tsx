import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { usePlaybookGraph } from "../../api/hooks";
import PlaybookGraphCanvas from "./PlaybookGraphCanvas";
import PlaybookNodeInspector from "./PlaybookNodeInspector";

/** The graph region is deliberately taller than the old JSON block so even a
 *  small playbook reads as a diagram. Class strings stay literal — Tailwind
 *  scans source text, so a composed `lg:${...}` would never be generated. */
function Notice({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-[60vh] items-center justify-center rounded-lg border border-gray-800 bg-gray-900/50 p-6">
      {children}
    </div>
  );
}

export interface PlaybookGraphViewProps {
  playbookId: string;
}

/** Graph tab body: owns the query's loading/error/empty states and the
 *  selected-node state. `PlaybookDetail` only picks the tab and hands over the
 *  playbook id; the canvas and inspector stay independently testable. */
export default function PlaybookGraphView({ playbookId }: PlaybookGraphViewProps) {
  const { data, isPending, isError, error, refetch } = usePlaybookGraph(playbookId);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);

  // A different playbook is a different graph — never carry a selection over.
  useEffect(() => setSelectedNodeId(null), [playbookId]);

  const nodes = useMemo(() => data?.graph?.nodes ?? [], [data]);
  const selected = useMemo(
    () => nodes.find((node) => node.id === selectedNodeId) ?? null,
    [nodes, selectedNodeId],
  );

  // Refreshed data may drop the inspected node; don't leave a stale panel open.
  useEffect(() => {
    if (selectedNodeId && !nodes.some((node) => node.id === selectedNodeId)) {
      setSelectedNodeId(null);
    }
  }, [nodes, selectedNodeId]);

  const onSelectNode = useCallback((nodeId: string | null) => setSelectedNodeId(nodeId), []);

  if (isPending) {
    return (
      <Notice>
        <p className="text-sm text-gray-400">Loading compiled graph…</p>
      </Notice>
    );
  }

  if (isError) {
    return (
      <Notice>
        <div className="max-w-md space-y-3 text-center">
          <p className="text-sm text-red-300">Could not load the compiled graph for this playbook.</p>
          <p className="break-words font-mono text-xs text-gray-500">
            {error instanceof Error ? error.message : String(error ?? "Unknown error")}
          </p>
          <button
            type="button"
            onClick={() => void refetch()}
            className="rounded-md bg-gray-800 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-700"
          >
            Retry
          </button>
        </div>
      </Notice>
    );
  }

  if (nodes.length === 0) {
    return (
      <Notice>
        <p className="text-sm text-gray-400">This compiled playbook has no nodes.</p>
      </Notice>
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-4 lg:h-[60vh] lg:flex-row">
      <div className="h-[60vh] min-w-0 flex-1 overflow-hidden rounded-lg border border-gray-800 bg-gray-950 lg:h-full">
        <PlaybookGraphCanvas
          graph={data?.graph}
          layout={data?.layout}
          selectedNodeId={selectedNodeId}
          onSelectNode={onSelectNode}
        />
      </div>
      <div className="max-h-[60vh] min-w-0 shrink-0 lg:h-full lg:max-h-full lg:w-96 lg:max-w-[40%]">
        <PlaybookNodeInspector node={selected} />
      </div>
    </div>
  );
}
