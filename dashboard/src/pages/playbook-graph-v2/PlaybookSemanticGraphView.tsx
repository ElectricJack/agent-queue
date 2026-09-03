import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { usePlaybookV2Graph } from "../../api/hooks";
import DiagnosticsBanner from "./DiagnosticsBanner";
import EventScopeSelector, { ALL_EVENTS } from "./EventScopeSelector";
import PlaybookSemanticGraphCanvas from "./PlaybookSemanticGraphCanvas";
import SemanticNodeInspector from "./SemanticNodeInspector";

function Notice({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-[60vh] items-center justify-center rounded-lg border border-gray-800 bg-gray-900/50 p-6">
      {children}
    </div>
  );
}

function shortHash(hash: string): string {
  const hex = hash.startsWith("sha256:") ? hash.slice("sha256:".length) : hash;
  return hex.slice(0, 12);
}

export interface PlaybookSemanticGraphViewProps {
  playbookId: string;
  /** Pin the view to one artifact instead of whichever is active. */
  artifactSha?: string;
}

/** Semantic graph tab body: event scope, canvas, inspector and diagnostics.
 *
 *  Owns the selected step, the selected event scope and the Advanced toggle.
 *  Advanced is deliberately held here rather than in the inspector, so an
 *  operator working through five nodes in Advanced mode does not re-open it
 *  five times. */
export default function PlaybookSemanticGraphView({
  playbookId,
  artifactSha,
}: PlaybookSemanticGraphViewProps) {
  const [eventType, setEventType] = useState<string>(ALL_EVENTS);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);

  const { data, isPending, isError, error, refetch } = usePlaybookV2Graph(playbookId, {
    artifactSha,
    eventType: eventType || undefined,
  });

  // A different playbook is a different graph — never carry a selection over.
  useEffect(() => {
    setSelectedNodeId(null);
    setEventType(ALL_EVENTS);
  }, [playbookId]);

  const nodes = useMemo(() => data?.nodes ?? [], [data]);
  const selected = useMemo(
    () => nodes.find((node) => node.id === selectedNodeId) ?? null,
    [nodes, selectedNodeId],
  );

  // Narrowing the event scope may drop the inspected step; don't leave a stale
  // panel open describing a step that is no longer on the canvas.
  useEffect(() => {
    if (selectedNodeId && !nodes.some((node) => node.id === selectedNodeId)) {
      setSelectedNodeId(null);
    }
  }, [nodes, selectedNodeId]);

  const onSelectNode = useCallback((nodeId: string | null) => setSelectedNodeId(nodeId), []);

  if (isPending) {
    return (
      <Notice>
        <p className="text-sm text-gray-400">Loading semantic graph…</p>
      </Notice>
    );
  }

  if (isError) {
    return (
      <Notice>
        <div className="max-w-md space-y-3 text-center">
          <p className="text-sm text-red-300">Could not load the semantic graph for this playbook.</p>
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

  const artifact = data.artifact;
  const activation = data.activation;
  const isActive = activation.active_artifact_sha256 === artifact.artifact_sha256;

  return (
    <div className="min-w-0 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <EventScopeSelector
          groups={data.event_groups ?? []}
          value={eventType}
          onChange={setEventType}
        />
        <dl className="flex flex-wrap items-center gap-3 text-[11px] text-gray-400">
          <div className="flex items-center gap-1">
            <dt className="text-gray-500">artifact</dt>
            <dd className="font-mono text-gray-300" title={artifact.artifact_sha256}>
              {shortHash(artifact.artifact_sha256)}
            </dd>
          </div>
          <div className="flex items-center gap-1">
            <dt className="text-gray-500">version</dt>
            <dd className="text-gray-300">v{artifact.version ?? 0}</dd>
          </div>
          <div className="flex items-center gap-1">
            <dt className="text-gray-500">health</dt>
            <dd className="rounded bg-gray-800 px-1.5 py-0.5 text-gray-200">{activation.health}</dd>
          </div>
          <div className="flex items-center gap-1">
            <dt className="text-gray-500">state</dt>
            <dd className="text-gray-300">
              {isActive ? (activation.enabled ? "active" : "active, disabled") : "not the active artifact"}
            </dd>
          </div>
        </dl>
      </div>

      <DiagnosticsBanner diagnostics={data.diagnostics ?? []} onSelectNode={onSelectNode} />

      <div className="flex min-w-0 flex-col gap-4 lg:h-[60vh] lg:flex-row">
        <div className="h-[60vh] min-w-0 flex-1 overflow-hidden rounded-lg border border-gray-800 bg-gray-950 lg:h-full">
          <PlaybookSemanticGraphCanvas
            graph={data}
            selectedNodeId={selectedNodeId}
            onSelectNode={onSelectNode}
          />
        </div>
        {selected && (
          <div className="max-h-[60vh] min-w-0 shrink-0 lg:h-full lg:max-h-full lg:w-[26rem] lg:max-w-[40%]">
            <SemanticNodeInspector
              node={selected}
              advanced={advanced}
              onAdvancedChange={setAdvanced}
            />
          </div>
        )}
      </div>
    </div>
  );
}
