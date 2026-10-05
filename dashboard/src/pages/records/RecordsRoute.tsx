import { useCallback, useEffect, useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { useLocation, useParams, useSearchParams } from "react-router-dom";
import { useKnowledgeCapabilities } from "../../api/knowledge";
import { recordShow } from "../../api/client";
import { useMediaQuery } from "../../hooks/useMediaQuery";
import { useRecordEdges, useRecordSearch, type RecordNode } from "../../api/records";
import KnowledgePane from "../../panes/knowledge/KnowledgePane";
import TaskDetailBody from "../../panes/task-detail/TaskDetailBody";
import KnowledgeWorkflow from "../knowledge/KnowledgeWorkflow";
import KnowledgeFilters from "../knowledge/KnowledgeFilters";
import { KindBadge } from "../knowledge/KnowledgeBadges";
import { createLiveKnowledgeAdapter, object } from "../knowledge/liveAdapter";
import CommandCenterTasks from "../command-center/Tasks";
import TaskToolbar from "../command-center/TaskToolbar";
import { readTaskFilters, writeTaskFilters } from "../command-center/taskFilters";
import RecordDetailPane from "./RecordDetailPane";
import RecordGraph from "./RecordGraph";
import { readRecordFilters, readRecordSelection, writeRecordFilters, writeRecordSelection } from "./recordUrlState";

export default function RecordsRoute() {
  const { projectId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const capabilities = useKnowledgeCapabilities(projectId);
  const available = capabilities.data?.available === true;
  const filters = readRecordFilters(params);
  const selection = readRecordSelection(params);
  const taskRecord = useQuery({ queryKey: ["records", "detail", projectId, selection?.recordId],
    enabled: available && selection?.kind === "task" && !selection.taskId,
    queryFn: async () => {
      const { data } = await recordShow({ body: { project_id: projectId, identity: `record:${selection?.recordId}` } });
      if (data?.kind !== "task" || typeof object(object(data).task).id !== "string") throw new Error("Task unavailable");
      return String(object(object(data).task).id);
    },
  });
  const selectedTaskId = selection?.kind === "task" ? selection.taskId ?? taskRecord.data : null;
  const taskOnly = filters.kind === "task" || (!available && !capabilities.isPending && filters.kind !== "knowledge");
  const graph = params.get("view") === "graph";
  const sheet = useMediaQuery("(max-width: 1023.98px)");
  const listRef = useRef<HTMLDivElement>(null);
  const adapter = useMemo(() => createLiveKnowledgeAdapter(projectId), [projectId]);
  const results = useRecordSearch(projectId, filters, available && !taskOnly);
  const nodes = results.data?.pages.flatMap((page) => page.items) ?? [];
  const graphNodes = nodes.slice(0, 100);
  const relationships = useRecordEdges(projectId, graphNodes, graph && available && !taskOnly);
  const selectKnowledge = (recordId: string, revisionId: string | null) => setParams((previous) =>
    writeRecordSelection(previous, { kind: "knowledge", recordId, revisionId }));
  const openTask = useCallback((taskId: string) => setParams((previous) =>
    writeRecordSelection(previous, { kind: "task", recordId: taskId, taskId, revisionId: null })), [setParams]);
  const close = useCallback(() => {
    if (!selection) return;
    const rows = Array.from(listRef.current?.querySelectorAll<HTMLElement>("[data-record-row], [data-task-row]") ?? []);
    const selected = rows.find((el) => el.getAttribute("aria-pressed") === "true" || el.getAttribute("aria-selected") === "true");
    setParams((previous) => writeRecordSelection(previous, null));
    requestAnimationFrame(() => selected?.focus({ preventScroll: true }));
  }, [selection, setParams]);
  useEffect(() => {
    if (!selection) return;
    const onEscape = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      const dialog = target.closest('[role="dialog"]');
      if (event.key === "Escape" && !event.defaultPrevented && (!dialog || dialog.hasAttribute("data-record-detail"))) {
        event.preventDefault(); close();
      }
    };
    document.addEventListener("keydown", onEscape);
    return () => document.removeEventListener("keydown", onEscape);
  }, [selection, close]);
  const select = (node: RecordNode, revisionId: string | null) => node.kind === "task"
    ? openTask(node.taskId) : selectKnowledge(node.recordId, revisionId);
  const selectedNode = nodes.find((node) => selection?.taskId ? node.kind === "task" && node.taskId === selection.taskId : node.recordId === selection?.recordId);
  const selectedId = selectedNode?.recordId ?? selection?.recordId;
  const fullHref = selection?.kind === "task"
    ? selectedTaskId ? `/tasks/${encodeURIComponent(selectedTaskId)}` : null
    : `/projects/${encodeURIComponent(projectId)}/knowledge/${encodeURIComponent(selection?.recordId ?? "")}${selection?.revisionId ? `?revision=${encodeURIComponent(selection.revisionId)}` : ""}`;

  return <KnowledgeWorkflow projectId={projectId}>{(onAction) => <div className="flex h-full min-h-0 min-w-0" data-record-workspace
    onKeyDownCapture={(event) => {
      const target = event.target as HTMLElement;
      const dialog = target.closest('[role="dialog"]');
      if (event.key === "Escape" && selection && (!dialog || dialog.hasAttribute("data-record-detail"))) {
        event.preventDefault(); event.stopPropagation(); close(); return;
      }
      if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || !listRef.current?.contains(target)
        || target.closest('input, textarea, select, [contenteditable="true"], [role="dialog"]')
        || !["ArrowDown", "ArrowUp", "j", "k"].includes(event.key)) return;
      const rows = Array.from(listRef.current.querySelectorAll<HTMLElement>("[data-record-row], [data-task-row]"));
      if (!rows.length) return;
      const focused = rows.findIndex((row) => row === target || row.contains(target));
      const chosen = rows.findIndex((row) => row.getAttribute("aria-pressed") === "true" || row.getAttribute("aria-selected") === "true");
      const index = focused >= 0 ? focused : chosen;
      const next = index < 0 ? (event.key === "ArrowUp" || event.key === "k" ? rows.length - 1 : 0)
        : Math.max(0, Math.min(rows.length - 1, index + (event.key === "ArrowUp" || event.key === "k" ? -1 : 1)));
      event.preventDefault(); event.stopPropagation();
      rows[next]?.focus(); rows[next]?.click();
    }}>
    <div ref={listRef} inert={sheet && !!selection ? true : undefined}
      className="flex min-h-0 min-w-0 flex-1 flex-col" data-record-list>
      <div className="shrink-0 space-y-3 border-b border-gray-800 p-3 md:p-4">
        <h1 className="text-lg font-semibold">Tasks & Knowledge</h1>
        <div className="flex flex-wrap items-center gap-3">
          <label className="text-sm">Record kind <select aria-label="Record kind" value={filters.kind}
            className="rounded border border-gray-700 bg-gray-900 p-1" onChange={(event) =>
              setParams((previous) => writeRecordFilters(previous, { ...filters, kind: event.target.value as typeof filters.kind }), { replace: true })}>
            <option value="all">All records</option><option value="task">Tasks</option><option value="knowledge" disabled={!available}>Knowledge</option>
          </select></label>
          {!taskOnly && available && (["list", "graph"] as const).map((view) => <button key={view} type="button" aria-pressed={graph === (view === "graph")}
            className="rounded border border-gray-700 px-3 py-1 text-sm" onClick={() => setParams((previous) => {
              const next = new URLSearchParams(previous);
              if (view === "graph") next.set("view", view); else next.delete("view");
              return next;
            })}>{view === "graph" ? "Graph" : "List"}</button>)}
        </div>
        {!taskOnly && available && <KnowledgeFilters searchLabel="Search records" compact={sheet} filters={filters} onChange={(next) => setParams((previous) =>
          writeRecordFilters(previous, { ...next, kind: filters.kind }), { replace: true })} />}
        {!taskOnly && available && <p className="hidden text-xs text-gray-500 lg:block">Category, lifecycle and verification filter knowledge. Knowledge results appear before tasks.</p>}
        {!capabilities.isPending && !available && <p role="status" className="text-xs text-gray-400">Knowledge is unavailable for this project. Tasks remain available.</p>}
      </div>
      {taskOnly ? <>
        <TaskToolbar onCreated={(taskId) => setParams((previous) => writeRecordSelection(
          writeTaskFilters(previous, { query: "", status: "", showCompleted: false, window: "", held: false,
            focus: readTaskFilters(previous).focus }),
          { kind: "task", recordId: taskId, taskId, revisionId: null },
        ))} />
        <div className="min-h-0 flex-1"><CommandCenterTasks selection={{ selectedTaskId: selectedTaskId ?? null,
          selectTask: (task) => openTask(task.id), clearTask: close }} /></div>
      </> : <div className="min-h-0 flex-1 overflow-y-auto p-3 md:p-4" data-record-scroll>
        {capabilities.isPending && <p role="status">Loading records…</p>}
        {available && results.isPending && <p role="status">Loading records…</p>}
        {results.isError && <p role="alert">Record search is temporarily unavailable.</p>}
        {available && results.isSuccess && (graph ? <>
          {relationships.some((query) => query.isPending) && <p role="status">Loading record links…</p>}
          {relationships.some((query) => query.isError) && <p role="alert">Some record links are unavailable.</p>}
          {nodes.length > 100 && <p>Graph shows the first 100 loaded records. Narrow the filters to see others.</p>}
          <RecordGraph nodes={graphNodes} edges={relationships.flatMap((query) => query.data ?? [])}
            selectedId={selectedId} onSelect={select} />
        </> : <ul aria-label="Record results" className="space-y-2">
          {nodes.length === 0 && <li>No records match these filters.</li>}
          {nodes.map((node) => <li key={node.recordId}><button type="button" data-record-row={node.recordId}
            aria-pressed={selectedId === node.recordId} data-record-kind={node.kind}
            className={`grid w-full grid-cols-[auto_minmax(0,1fr)] items-center gap-x-3 gap-y-1 rounded border p-3 text-left text-sm focus-visible:outline-indigo-400 ${selectedId === node.recordId ? "border-indigo-400 bg-indigo-500/15" : "border-gray-800 bg-gray-900"}`}
            onClick={() => select(node, null)}>
            <KindBadge kind={node.kind} /><span className="min-w-0 flex-1 break-words">{node.title}</span>
            <span className="col-start-2 text-xs text-gray-400">{node.kind === "task" ? `${node.status}${node.archived ? " · archived" : ""}`
              : `${node.category} · ${node.lifecycle} · ${node.verification}`}</span>
          </button></li>)}
        </ul>)}
        {results.hasNextPage && <button type="button" disabled={results.isFetchingNextPage}
          onClick={() => void results.fetchNextPage()}>Load more records</button>}
      </div>}
    </div>
    {selection && <RecordDetailPane sheet={sheet} fullHref={fullHref} from={location.pathname + location.search} onClose={close}>
      {selection.kind === "knowledge" ? available ? <KnowledgePane key={selection.recordId} adapter={adapter}
        recordId={selection.recordId} revisionId={selection.revisionId}
        onRevisionChange={(revisionId) => selectKnowledge(selection.recordId, revisionId)}
        onOpenRecord={selectKnowledge} onOpenTask={openTask} onAction={onAction} />
        : <p role="status" className="p-4">Knowledge is unavailable for this project.</p>
        : selectedTaskId ? <TaskDetailBody key={selectedTaskId} taskId={selectedTaskId} onOpenTask={openTask} onClose={close} onLeave={() => {}} />
        : taskRecord.isError || !available ? <p role="alert" className="p-4">This task is unavailable.</p>
        : <p role="status" className="p-4">Loading task…</p>}
    </RecordDetailPane>}
  </div>}</KnowledgeWorkflow>;
}
