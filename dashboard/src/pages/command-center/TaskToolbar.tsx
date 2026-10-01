import { useCallback, useId, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { AdjustmentsHorizontalIcon, MagnifyingGlassIcon, PlusIcon, XMarkIcon } from "@heroicons/react/24/outline";
import { isCompactViewport } from "../../hooks/useCompactViewport";
import CreateTaskModal from "../../components/CreateTaskModal";
import { fetchRunningTarget, useTidyLayout } from "../../api/graphLayout";
import { useAppliedVariant } from "./layout-v2/appliedVariant";
import { useJumpToResult } from "./layout-v2/useJumpToResult";
import { publishRunningWorkJump, publishRunningWorkNotice, useRunningWorkNotice } from "./layout-v2/runningWork";
import { useShellPaneStore } from "../../panes/store";
import { useShortcut } from "../../shell/hotkeys/useShortcuts";
import { useTaskWorkspace } from "./TaskWorkspace";
import { useGraphState } from "./useGraphHierarchy";
import { DEFAULT_DENSITY, type LayoutDensity } from "./layout-v2/density";
import { ACTIVITY_WINDOWS, FINISHED_STATUSES, TASK_STATUSES, taskStatusLabel } from "./taskFilters";

export default function TaskToolbar() {
  const { projectId, filters, focusId, setQuery, setStatus, setShowCompleted, setWindow, setHeld, clearFilters, goToRunningWork } = useTaskWorkspace();
  // The variant the canvas was actually SERVED, not the one the filters ask
  // for: the daemon promotes a focused request to the full layout when the
  // entered container is not in the active one, and searching that container
  // against `active` would find nothing. Before the canvas has answered (or
  // on the Tasks tab, where it is not mounted) the filters are the best
  // guess there is.
  const served = useAppliedVariant();
  const variant = served ?? (filters.showCompleted ? "all" : "active");
  // Only the graph pans to a hit, and only a server-side layout knows where
  // one is: on the Tasks tab the control would do nothing, so it is not shown
  // and the `locate` request is never issued.
  const onGraph = useLocation().pathname.endsWith("/graph");
  const { next: jumpNext, count: jumpCount } = useJumpToResult(
    onGraph ? projectId : undefined, variant, filters, focusId);
  const { density, setDensity } = useGraphState();
  const tidy = useTidyLayout(projectId ?? "");
  const filtersId = useId();
  const [filtersOpen, setFiltersOpen] = useState(() => !isCompactViewport());
  const [createOpen, setCreateOpen] = useState(false);
  const [findingRunningWork, setFindingRunningWork] = useState(false);
  const runningWorkNotice = useRunningWorkNotice();
  const searchRef = useRef<HTMLInputElement>(null);
  const pane = useShellPaneStore();
  const shortcutsAvailable = () => !createOpen && !document.querySelector('[role="dialog"], [aria-modal="true"]');
  useShortcut("n", { label: "add task", section: "Tasks", onFire: () => setCreateOpen(true), when: shortcutsAvailable });
  useShortcut("/", { label: "search tasks", section: "Tasks", onFire: () => searchRef.current?.focus(), when: shortcutsAvailable });
  const secondaryFilters = [filters.window, filters.showCompleted && !filters.window, !onGraph && filters.held].filter(Boolean).length;
  const hasFilters = !!(filters.query || filters.status || filters.showCompleted || filters.window || filters.held);
  const takeToRunningWork = useCallback(async () => {
    setFindingRunningWork(true);
    publishRunningWorkNotice(null);
    try {
      const target = await fetchRunningTarget(projectId);
      if (target === null) {
        publishRunningWorkNotice("No running work");
        return;
      }
      publishRunningWorkJump(target);
      goToRunningWork(target);
    } catch {
      publishRunningWorkNotice("Could not find running work. Try again.");
    } finally {
      setFindingRunningWork(false);
    }
  }, [projectId, goToRunningWork]);

  return (
    <div className="shrink-0 space-y-2 border-b border-gray-800 bg-gray-950 px-3 py-3 md:px-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-0 basis-full md:basis-auto md:flex-1">
          <MagnifyingGlassIcon className="pointer-events-none absolute left-3 top-3.5 md:top-2.5 h-4 w-4 text-gray-500" />
          <input ref={searchRef} type="search" aria-label="Search tasks" value={filters.query}
            onChange={(e) => setQuery(e.target.value)} placeholder="Search tasks…"
            data-primary-control className="h-11 w-full rounded-md md:h-9 border border-gray-700 bg-gray-900 pl-8 pr-8 text-sm text-gray-100 placeholder:text-gray-500 focus:border-indigo-500 focus:outline-none" />
          <kbd className="pointer-events-none absolute right-3 top-3 md:top-2 text-xs text-gray-500">/</kbd>
        </div>
        <select aria-label="Task status" value={filters.status} onChange={(e) => setStatus(e.target.value)}
          data-primary-control className="h-11 min-w-0 flex-1 rounded-md md:h-9 md:max-w-48 md:flex-none border border-gray-700 bg-gray-900 px-2 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none">
          <option value="">All statuses</option>
          {filters.status && !TASK_STATUSES.includes(filters.status) && <option value={filters.status}>{taskStatusLabel(filters.status)}</option>}
          {TASK_STATUSES.map((status) => <option key={status} value={status}>{taskStatusLabel(status)}</option>)}
        </select>
        <button type="button" data-primary-control aria-expanded={filtersOpen} aria-controls={filtersId}
          aria-label={secondaryFilters ? `Filters (${secondaryFilters} active)` : "Filters"}
          onClick={() => setFiltersOpen((open) => !open)}
          className={`focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-400 inline-flex h-11 shrink-0 items-center gap-1.5 rounded-md border px-3 text-sm md:h-9 ${filtersOpen || secondaryFilters ? "border-indigo-400/50 bg-indigo-500/10 text-indigo-200" : "border-gray-700 text-gray-300 hover:bg-gray-800"}`}>
          <AdjustmentsHorizontalIcon className="h-4 w-4" /> Filters{secondaryFilters > 0 && <span aria-hidden="true" className="rounded bg-indigo-400/20 px-1.5 text-xs">{secondaryFilters}</span>}
        </button>
        <button type="button" data-primary-control onClick={() => setCreateOpen(true)} title="Add task (N)"
          className="ml-auto inline-flex h-11 shrink-0 md:h-9 items-center gap-1.5 rounded-md bg-indigo-600 px-3 text-sm font-medium text-white hover:bg-indigo-500">
          <PlusIcon className="h-4 w-4" /> Add task <kbd className="ml-1 hidden text-xs text-indigo-200 md:inline">N</kbd>
        </button>
      </div>
      <div id={filtersId} hidden={!filtersOpen} className={filtersOpen ? "flex flex-wrap items-center gap-2 border-t border-gray-800/70 pt-2" : "hidden"}>
        <select aria-label="Time range" value={filters.window} onChange={(e) => setWindow(e.target.value)}
          title="Limit the list to tasks worked on recently, and show which models did the work"
          data-primary-control className="h-11 max-w-full md:h-9 md:max-w-48 rounded-md border border-gray-700 bg-gray-900 px-2 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none">
          <option value="">Any time</option>
          {ACTIVITY_WINDOWS.map((w) => <option key={w.key} value={w.key}>{w.label}</option>)}
        </select>
        <label className="flex min-h-11 md:min-h-9 items-center gap-2 px-1 text-xs text-gray-400" title={filters.window ? "Included with a time range" : undefined}>
          <input type="checkbox" checked={filters.showCompleted || FINISHED_STATUSES.has(filters.status)}
            disabled={!!filters.window} onChange={(e) => setShowCompleted(e.target.checked)} className="accent-indigo-500 disabled:opacity-50" />
          Show completed
        </label>
        {/* The held filter narrows the Tasks table to the server's held-task
            ids; the graph draws from its own layout and has no such filter. */}
        {!onGraph && <label className="flex min-h-11 md:min-h-9 items-center gap-2 px-1 text-xs text-gray-400"
          title="Only tasks waiting on an unavailable provider">
          <input type="checkbox" checked={filters.held} onChange={(e) => setHeld(e.target.checked)} className="accent-indigo-500" />
          Held by provider
        </label>}
        {onGraph && jumpCount > 0 && <button type="button" onClick={jumpNext}
          title="Pan the graph to the next matching task"
          data-primary-control className="h-11 md:h-9 rounded-md border border-gray-700 px-3 text-xs text-gray-200 hover:bg-gray-800">
          Next result ({jumpCount})
        </button>}
        {onGraph && <button type="button" onClick={() => void takeToRunningWork()} disabled={findingRunningWork}
          title="Enter the container holding the highest-priority running task"
          data-primary-control className="h-11 md:h-9 rounded-md border border-gray-700 px-3 text-xs text-gray-200 hover:bg-gray-800 disabled:opacity-50">
          {findingRunningWork ? "Finding running work…" : "Running work"}
        </button>}
        {onGraph && runningWorkNotice && <span role="status" className="text-xs text-gray-400">{runningWorkNotice}</span>}
        {hasFilters && <button type="button" aria-label="Clear task filters" title="Clear filters" onClick={clearFilters}
          data-primary-control className="inline-flex h-11 items-center gap-1 rounded px-2 md:h-9 text-gray-300 hover:bg-gray-800 hover:text-gray-100"><XMarkIcon className="h-4 w-4" /> Clear filters</button>}
        {onGraph && <label className="flex min-h-11 md:min-h-9 items-center rounded-md border border-gray-700 px-3 text-xs text-gray-200">
          Density
          <select aria-label="Graph density" value={density} onChange={(e) => setDensity(e.target.value as LayoutDensity)}
            className="ml-2 bg-transparent text-xs text-white outline-none">
            <option value="compact">Compact</option>
            <option value={DEFAULT_DENSITY}>Comfortable</option>
            <option value="spacious">Spacious</option>
          </select>
        </label>}
        {projectId && <button type="button" disabled={tidy.isPending}
          title="Re-arrange every node in this project"
          onClick={() => { if (window.confirm("Tidy re-arranges every node in this project. Continue?")) tidy.mutate(); }}
          data-primary-control className="h-11 md:h-9 rounded-md border border-gray-700 px-3 text-xs text-gray-200 hover:bg-gray-800 disabled:opacity-50">
          {tidy.isPending ? "Tidying…" : "Tidy layout"}
        </button>}
        {tidy.isError && <span role="alert" className="text-xs text-amber-200">Tidy failed. Try again.</span>}
      </div>
      {createOpen && <CreateTaskModal key={projectId ?? "all"} open onClose={() => setCreateOpen(false)} defaultProjectId={projectId}
        onCreated={(taskId) => { clearFilters(); pane.open("task-detail", { taskId }); }} />}
    </div>
  );
}
