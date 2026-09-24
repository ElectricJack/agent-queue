import { useEffect, useRef } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useSessions } from "../../api/hooks";
import { SessionPager } from "../../components/SessionPager";
import { useSessionPage } from "../../hooks/useSessionPage";

const COLUMNS = 5;

export default function ProjectSessions() {
  const { projectId = "" } = useParams();
  const location = useLocation();
  const { page, setPage } = useSessionPage();
  const { data, isLoading, error } = useSessions(projectId, page);
  const sessions = data?.sessions ?? [];
  const scrollRef = useRef<HTMLDivElement>(null);
  // Each poll changes every row's idle time, so rendering a whole page of
  // session history made every refresh a long task; only the rows in view
  // (plus overscan) are mounted.
  const virtualizer = useVirtualizer({
    count: sessions.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 41,
    overscan: 12,
  });
  const items = virtualizer.getVirtualItems();
  const padTop = items.length ? items[0]!.start : 0;
  const padBottom = items.length
    ? virtualizer.getTotalSize() - items[items.length - 1]!.end
    : 0;

  // The scroll container outlives a page change; start each page at its top.
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [page]);

  return (
    <div className="flex h-full min-h-0 flex-col gap-4">
      {isLoading && <p className="text-sm text-gray-400">Loading…</p>}
      {error && <p className="text-sm text-red-400">Failed to load sessions: {error.message}</p>}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto rounded border border-gray-800">
        <table className="w-full text-sm">
          <thead className="sticky top-0 z-10 bg-gray-900 text-left text-xs uppercase tracking-wider text-gray-500">
            <tr>
              <th className="px-3 py-2">Name</th>
              <th className="px-3 py-2">Task</th>
              <th className="px-3 py-2">Harness</th>
              <th className="px-3 py-2">State</th>
              <th className="px-3 py-2">Idle</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800">
            {padTop > 0 && <tr aria-hidden="true"><td colSpan={COLUMNS} style={{ height: padTop, padding: 0, border: 0 }} /></tr>}
            {items.map((item) => {
              const s = sessions[item.index]!;
              return (
                <tr key={s.id} data-index={item.index} ref={virtualizer.measureElement} className="hover:bg-gray-900">
                  <td className="px-3 py-2">
                    <Link
                      to={`/sessions/${encodeURIComponent(s.id)}`}
                      state={{ from: location.pathname + location.search, terminalFocus: true }}
                      className="text-indigo-400 hover:text-indigo-300"
                    >
                      {s.name}
                    </Link>
                  </td>
                  <td className="px-3 py-2 text-gray-400">{s.task_id ?? "—"}</td>
                  <td className="px-3 py-2 text-gray-400">{s.harness ?? "—"}</td>
                  <td className="px-3 py-2">
                    <span
                      className={`rounded px-2 py-0.5 text-xs ${
                        s.stalled ? "bg-amber-500/10 text-amber-400" : "bg-gray-800 text-gray-300"
                      }`}
                    >
                      {s.state ?? "?"}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-gray-400">
                    {Math.round(s.idle_seconds ?? 0)}s
                  </td>
                </tr>
              );
            })}
            {padBottom > 0 && <tr aria-hidden="true"><td colSpan={COLUMNS} style={{ height: padBottom, padding: 0, border: 0 }} /></tr>}
            {sessions.length === 0 && !isLoading && !error && (
              <tr>
                <td colSpan={COLUMNS} className="px-3 py-6 text-center text-gray-500">
                  {page === 0 ? "No sessions for this project." : "No sessions on this page."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <SessionPager page={page} setPage={setPage} hasMore={data?.hasMore ?? false} loading={isLoading} />
    </div>
  );
}
