import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import TaskCard from "../../components/TaskCard";
import { useTaskListRows } from "../command-center/useTaskListRows";
import { useTaskWorkspace } from "../command-center/TaskWorkspace";
import {
  FINISHED_STATUSES, readTaskFilters, TASK_STATUSES, taskStatusLabel, writeTaskFilters, type TaskFilters,
} from "../command-center/taskFilters";
import { focusTaskHref } from "./routes";

export const PAGE_SIZE = 50;

const CONTROL = "rounded-md border border-gray-700 bg-gray-900 px-2 text-sm text-gray-200";

/**
 * The Tasks tab's rows as cards, 50 per page (mobile dashboard §4.2). Filters,
 * project and page are URL state, edited in place (replace), so Back from a
 * task returns to exactly this page of this list; FocusShell restores the
 * scroll. A filter change starts again at page 1.
 */
export default function FocusTaskList() {
  const { projects, projectId } = useTaskWorkspace();
  const { rows, isLoading, error, names } = useTaskListRows();
  const [params, setParams] = useSearchParams();
  const filters = readTaskFilters(params);
  // The router applies a URL edit in a transition, so a box echoing `q` would
  // snap back between fast keystrokes and drop them. The box owns its text
  // from mount (Back remounts the page with the URL's) and writes the URL.
  const [query, setQuery] = useState(filters.query);
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const requested = Number.parseInt(params.get("page") ?? "1", 10);
  const page = Math.min(pages, Math.max(1, Number.isFinite(requested) ? requested : 1));
  const shown = rows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  // One setParams per edit: consecutive functional updates in one tick would
  // each read the same previous value.
  const edit = (change: (next: URLSearchParams) => URLSearchParams) =>
    setParams((previous) => change(new URLSearchParams(previous)), { replace: true });
  const setFilter = (patch: Partial<TaskFilters>) =>
    edit((next) => {
      const written = writeTaskFilters(next, { ...readTaskFilters(next), ...patch });
      written.delete("page");
      return written;
    });
  // As TaskWorkspace.setStatus does: a finished status needs finished tasks shown.
  const setStatus = (status: string) =>
    setFilter({ status, ...(FINISHED_STATUSES.has(status) ? { showCompleted: true } : {}) });
  const setPage = (target: number) =>
    edit((next) => {
      if (target <= 1) next.delete("page");
      else next.set("page", String(target));
      return next;
    });
  const setProject = (id: string) =>
    edit((next) => {
      if (id) next.set("project", id);
      else next.delete("project");
      next.delete("page");
      return next;
    });

  return (
    <section aria-labelledby="focus-tasks" className="space-y-2">
      <h2 id="focus-tasks" className="text-xs uppercase tracking-wide text-gray-500">Tasks</h2>
      <div className="flex flex-wrap items-center gap-2">
        <input type="search" aria-label="Search tasks" placeholder="Search tasks" value={query}
          onChange={(event) => { setQuery(event.target.value); setFilter({ query: event.target.value }); }}
          data-primary-control className={`${CONTROL} min-w-0 flex-1 basis-40`} />
        <select aria-label="Status" value={filters.status} onChange={(event) => setStatus(event.target.value)}
          data-primary-control className={CONTROL}>
          <option value="">Any status</option>
          {TASK_STATUSES.map((status) => <option key={status} value={status}>{taskStatusLabel(status)}</option>)}
        </select>
        <select aria-label="Project" value={projectId ?? ""} onChange={(event) => setProject(event.target.value)}
          data-primary-control className={`${CONTROL} max-w-full`}>
          <option value="">All projects</option>
          {projects.map((project) => <option key={project.id} value={project.id}>{project.name || project.id}</option>)}
        </select>
      </div>
      <p className="text-xs text-gray-500">{rows.length} {rows.length === 1 ? "task" : "tasks"} · page {page} of {pages}</p>
      {error && <p role="alert" className="rounded border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">Could not load tasks. Check the connection and retry.</p>}
      {isLoading ? (
        <p role="status" className="text-sm text-gray-500">Loading tasks…</p>
      ) : shown.length === 0 ? (
        <p className="text-sm text-gray-500">No tasks match these filters.</p>
      ) : (
        <ul aria-label="Tasks" className="space-y-2">
          {shown.map((task) => (
            <li key={task.id}>
              <TaskCard task={task} to={focusTaskHref(task.id)}
                projectName={projectId ? undefined : names.get(task.project_id ?? "") || task.project_id} />
            </li>
          ))}
        </ul>
      )}
      <nav aria-label="Task pages" className="flex items-center justify-between gap-2 pt-1">
        <button type="button" aria-label="Previous page" data-primary-control disabled={page <= 1}
          onClick={() => setPage(page - 1)} className={`${CONTROL} disabled:opacity-40`}>Previous</button>
        <span className="text-xs text-gray-400">{page} / {pages}</span>
        <button type="button" aria-label="Next page" data-primary-control disabled={page >= pages}
          onClick={() => setPage(page + 1)} className={`${CONTROL} disabled:opacity-40`}>Next</button>
      </nav>
    </section>
  );
}
