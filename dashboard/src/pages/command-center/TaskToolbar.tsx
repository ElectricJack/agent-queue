import { useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { MagnifyingGlassIcon, PlusIcon, XMarkIcon } from "@heroicons/react/24/outline";
import CreateTaskModal from "../../components/CreateTaskModal";
import { useTidyLayout } from "../../api/graphLayout";
import { useJumpToResult } from "./layout-v2/useJumpToResult";
import { useShellPaneStore } from "../../panes/store";
import { useShortcut } from "../../shell/hotkeys/useShortcuts";
import { useTaskWorkspace } from "./TaskWorkspace";
import { FINISHED_STATUSES, TASK_STATUSES, taskStatusLabel } from "./taskFilters";

export default function TaskToolbar() {
  const { projectId, filters, focusId, setQuery, setStatus, setShowCompleted, clearFilters } = useTaskWorkspace();
  const variant = filters.showCompleted || focusId ? "all" : "active";
  // Only the graph pans to a hit, and only a server-side layout knows where
  // one is: on the Tasks tab the control would do nothing, so it is not shown
  // and the `locate` request is never issued.
  const onGraph = useLocation().pathname.endsWith("/graph");
  const { next: jumpNext, count: jumpCount } = useJumpToResult(
    onGraph ? projectId : undefined, variant, filters);
  const tidy = useTidyLayout(projectId ?? "");
  const [createOpen, setCreateOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const pane = useShellPaneStore();
  const shortcutsAvailable = () => !createOpen && !document.querySelector('[role="dialog"], [aria-modal="true"]');
  useShortcut("n", { label: "add task", section: "Tasks", onFire: () => setCreateOpen(true), when: shortcutsAvailable });
  useShortcut("/", { label: "search tasks", section: "Tasks", onFire: () => searchRef.current?.focus(), when: shortcutsAvailable });
  const hasFilters = !!(filters.query || filters.status || filters.showCompleted);

  return (
    <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-gray-800 bg-gray-950 px-4 py-3">
      <div className="relative min-w-44 flex-1">
        <MagnifyingGlassIcon className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-gray-500" />
        <input ref={searchRef} type="search" aria-label="Search tasks" value={filters.query}
          onChange={(e) => setQuery(e.target.value)} placeholder="Search tasks…"
          className="h-9 w-full rounded-md border border-gray-700 bg-gray-900 pl-8 pr-8 text-sm text-gray-100 placeholder:text-gray-500 focus:border-indigo-500 focus:outline-none" />
        <kbd className="pointer-events-none absolute right-3 top-2 text-xs text-gray-500">/</kbd>
      </div>
      <select aria-label="Task status" value={filters.status} onChange={(e) => setStatus(e.target.value)}
        className="h-9 max-w-48 rounded-md border border-gray-700 bg-gray-900 px-2 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none">
        <option value="">All statuses</option>
        {filters.status && !TASK_STATUSES.includes(filters.status) && <option value={filters.status}>{taskStatusLabel(filters.status)}</option>}
        {TASK_STATUSES.map((status) => <option key={status} value={status}>{taskStatusLabel(status)}</option>)}
      </select>
      <label className="flex h-9 items-center gap-2 px-1 text-xs text-gray-400" title={focusId ? "Disabled while focused" : undefined}>
        <input type="checkbox" checked={filters.showCompleted || FINISHED_STATUSES.has(filters.status)}
          disabled={!!focusId} onChange={(e) => setShowCompleted(e.target.checked)} className="accent-indigo-500 disabled:opacity-50" />
        Show completed
      </label>
      {onGraph && jumpCount > 0 && <button type="button" onClick={jumpNext}
        title="Pan the graph to the next matching task"
        className="h-9 rounded-md border border-gray-700 px-3 text-xs text-gray-200 hover:bg-gray-800">
        Next result ({jumpCount})
      </button>}
      {hasFilters && <button type="button" aria-label="Clear task filters" title="Clear filters" onClick={clearFilters}
        className="rounded p-2 text-gray-400 hover:bg-gray-800 hover:text-gray-100"><XMarkIcon className="h-4 w-4" /></button>}
      {projectId && <button type="button" disabled={tidy.isPending}
        title="Re-arrange every node in this project"
        onClick={() => { if (window.confirm("Tidy re-arranges every node in this project. Continue?")) tidy.mutate(); }}
        className="h-9 rounded-md border border-gray-700 px-3 text-xs text-gray-200 hover:bg-gray-800 disabled:opacity-50">
        {tidy.isPending ? "Tidying…" : "Tidy layout"}
      </button>}
      {tidy.isError && <span role="alert" className="text-xs text-amber-200">Tidy failed. Try again.</span>}
      <button type="button" onClick={() => setCreateOpen(true)} title="Add task (N)"
        className="inline-flex h-9 items-center gap-1.5 rounded-md bg-indigo-600 px-3 text-sm font-medium text-white hover:bg-indigo-500">
        <PlusIcon className="h-4 w-4" /> Add task <kbd className="ml-1 text-xs text-indigo-200">N</kbd>
      </button>
      {createOpen && <CreateTaskModal key={projectId ?? "all"} open onClose={() => setCreateOpen(false)} defaultProjectId={projectId}
        onCreated={(taskId) => { clearFilters(); pane.open("task-detail", { taskId }); }} />}
    </div>
  );
}
