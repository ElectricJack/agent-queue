import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useKnowledgeCapabilities } from "../../api/knowledge";
import { recordShow } from "../../api/client";
import { useRecordEdges, useRecordSearch, type RecordNode } from "../../api/records";
import KnowledgePane from "../../panes/knowledge/KnowledgePane";
import KnowledgeWorkflow from "../knowledge/KnowledgeWorkflow";
import KnowledgeFilters from "../knowledge/KnowledgeFilters";
import { KindBadge } from "../knowledge/KnowledgeBadges";
import { createLiveKnowledgeAdapter, object } from "../knowledge/liveAdapter";
import { useListNav } from "../../shell/hotkeys/useListNav";
import RecordGraph from "./RecordGraph";
import { readRecordFilters, readRecordSelection, writeRecordFilters, writeRecordSelection } from "./recordUrlState";

function SelectedTask({ projectId, recordId, onOpen }: { projectId: string; recordId: string; onOpen: (id: string) => void }) {
  const detail = useQuery({ queryKey: ["records", "detail", projectId, recordId], queryFn: async () => {
    const { data } = await recordShow({ body: { project_id: projectId, identity: `record:${recordId}` } });
    if (data?.kind !== "task") throw new Error("Task unavailable");
    return object(object(data).task);
  } });
  if (detail.isPending) return <p role="status">Loading task…</p>;
  if (detail.isError) return <p role="alert">This task is unavailable.</p>;
  return <button type="button" onClick={() => onOpen(String(detail.data.id))}>Open task {String(detail.data.title)}</button>;
}

export default function RecordsRoute() {
  const { projectId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const capabilities = useKnowledgeCapabilities(projectId);
  const available = capabilities.data?.available === true;
  const filters = readRecordFilters(params);
  const selection = readRecordSelection(params);
  const graph = params.get("view") === "graph";
  const adapter = useMemo(() => createLiveKnowledgeAdapter(projectId), [projectId]);
  const results = useRecordSearch(projectId, filters, available);
  const nodes = results.data?.pages.flatMap((page) => page.items) ?? [];
  const graphNodes = nodes.slice(0, 100);
  const relationships = useRecordEdges(projectId, graphNodes, graph && available);
  const openTask = (taskId: string) => navigate(`/tasks/${encodeURIComponent(taskId)}`, {
    state: { from: `${location.pathname}${location.search}` },
  });
  const selectKnowledge = (recordId: string, revisionId: string | null) => setParams((previous) =>
    writeRecordSelection(previous, { kind: "knowledge", recordId, revisionId }));
  const select = (node: RecordNode, revisionId: string | null) => node.kind === "task"
    ? openTask(node.taskId) : selectKnowledge(node.recordId, revisionId);
  if (capabilities.isPending) return <p role="status">Loading records…</p>;
  if (!available) return <p role="status">All records is unavailable for this project.</p>;

  return <KnowledgeWorkflow projectId={projectId}>{(onAction) => <div className="space-y-4">
    <h1 className="text-lg font-semibold">All records</h1>
    <div className="flex flex-wrap items-center gap-3">
      <label className="text-sm">Record kind <select aria-label="Record kind" value={filters.kind}
        className="rounded border border-gray-700 bg-gray-900 p-1" onChange={(event) =>
          setParams((previous) => writeRecordFilters(previous, { ...filters, kind: event.target.value as typeof filters.kind }), { replace: true })}>
        <option value="all">All records</option><option value="task">Tasks</option><option value="knowledge">Knowledge</option>
      </select></label>
      {(["list", "graph"] as const).map((view) => <button key={view} type="button" aria-pressed={graph === (view === "graph")}
        className="rounded border border-gray-700 px-3 py-1 text-sm" onClick={() => setParams((previous) => {
          const next = new URLSearchParams(previous);
          if (view === "graph") next.set("view", view); else next.delete("view");
          return next;
        })}>{view === "graph" ? "Graph" : "List"}</button>)}
    </div>
    <KnowledgeFilters searchLabel="Search records" filters={filters} onChange={(next) => setParams((previous) =>
      writeRecordFilters(previous, { ...next, kind: filters.kind }), { replace: true })} />
    <p className="text-xs text-gray-500">Category, lifecycle and verification filter knowledge. Knowledge results appear before tasks.</p>
    {results.isPending && <p role="status">Loading records…</p>}
    {results.isError && <p role="alert">Record search is temporarily unavailable.</p>}
    {results.isSuccess && (graph ? <>
      {relationships.some((query) => query.isPending) && <p role="status">Loading record links…</p>}
      {relationships.some((query) => query.isError) && <p role="alert">Some record links are unavailable.</p>}
      {nodes.length > 100 && <p>Graph shows the first 100 loaded records. Narrow the filters to see others.</p>}
      <RecordGraph nodes={graphNodes} edges={relationships.flatMap((query) => query.data ?? [])}
        selectedId={selection?.recordId} onSelect={select} />
    </> : <RecordResults nodes={nodes} selectedId={selection?.recordId} onSelect={select} />)}
    {results.hasNextPage && <button type="button" disabled={results.isFetchingNextPage}
      onClick={() => void results.fetchNextPage()}>Load more records</button>}
    {selection?.kind === "knowledge" && <KnowledgePane key={selection.recordId} adapter={adapter}
      recordId={selection.recordId} revisionId={selection.revisionId}
      onRevisionChange={(revisionId) => selectKnowledge(selection.recordId, revisionId)}
      onOpenRecord={selectKnowledge} onOpenTask={openTask} onAction={onAction} />}
    {selection?.kind === "task" && <SelectedTask projectId={projectId} recordId={selection.recordId} onOpen={openTask} />}
  </div>}</KnowledgeWorkflow>;
}

function RecordResults({ nodes, selectedId, onSelect }: { nodes: RecordNode[]; selectedId?: string; onSelect: (node: RecordNode, revisionId: string | null) => void }) {
  const listRef = useListNav<HTMLUListElement>();
  return <ul ref={listRef} aria-label="Record results" className="space-y-2">
      {nodes.length === 0 && <li>No records match these filters.</li>}
      {nodes.map((node) => <li key={node.recordId}><button type="button" data-listnav="1"
        aria-pressed={selectedId === node.recordId} data-record-kind={node.kind}
        className="flex w-full items-center gap-3 rounded border border-gray-800 bg-gray-900 p-3 text-left text-sm"
        onClick={() => onSelect(node, null)}>
        <KindBadge kind={node.kind} /><span className="min-w-0 flex-1 break-words">{node.title}</span>
        <span className="text-xs text-gray-400">{node.kind === "task" ? `${node.status}${node.archived ? " · archived" : ""}`
          : `${node.category} · ${node.lifecycle} · ${node.verification}`}</span>
      </button></li>)}
    </ul>;
}
