import { useCallback, useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { ProjectGraphResponse } from "@aq/ts-client";
import { useEventStream, type ConnectionStatus } from "../../ws/useEventStream";
import type { NotifyEvent } from "../../ws/types";
import { projectGraphKey } from "../../api/graph";
import { refetchLayout } from "./layout-v2/liveRegistry";
import { layoutExtentPrefix } from "../../api/graphLayout";
import type { GraphTaskNode } from "./types";

// Coalesce a burst, but do not postpone indefinitely while agents keep working.
const REFRESH_WINDOW_MS = 500;
type PendingRefresh = {
  timer?: ReturnType<typeof setTimeout>;
  fetching: boolean;
  dirty: boolean;
};

/** Keep the shared task/graph snapshots current without changing view selection. */
export function useGraphLive(projectIds: string[]) {
  const qc = useQueryClient();
  const selectedRef = useRef(projectIds);
  selectedRef.current = projectIds;
  const pendingRef = useRef(new Map<string, PendingRefresh>());

  const refresh = useCallback(function schedule(ids: string[]) {
    const pending = pendingRef.current;
    for (const pid of ids) {
      const current = pending.get(pid);
      if (current) {
        // Events during a request need a follow-up snapshot, not another
        // cancellation: a busy stream must let even a slow request finish.
        if (current.fetching) current.dirty = true;
        continue;
      }
      const entry: PendingRefresh = { fetching: false, dirty: false };
      entry.timer = setTimeout(async () => {
        entry.timer = undefined;
        entry.fetching = true;
        try {
          if (!selectedRef.current.includes(pid)) return;
          const filters = { queryKey: projectGraphKey(pid), exact: true };
          // An event can arrive during the first fetch, when invalidation
          // alone would reuse an older snapshot and lose the change.
          await qc.cancelQueries(filters);
          if (pending.get(pid) === entry && selectedRef.current.includes(pid)) {
            await qc.invalidateQueries(filters);
            // The tiled layout lives outside React Query's cache, so mounted
            // layers are told separately — on the same coalesced schedule.
            refetchLayout(pid);
            // The extent is a cached query and drives the status strip's task
            // count and the canvas bounds; without this it only refreshed on
            // its 60 s poll, so the strip lagged a completed task by a minute.
            await qc.invalidateQueries({ queryKey: layoutExtentPrefix(pid) });
          }
        } finally {
          if (pending.get(pid) === entry) {
            pending.delete(pid);
            if (entry.dirty && selectedRef.current.includes(pid)) schedule([pid]);
          }
        }
      }, REFRESH_WINDOW_MS);
      pending.set(pid, entry);
    }
  }, [qc]);

  useEffect(() => {
    const pending = pendingRef.current;
    return () => {
      for (const entry of pending.values()) clearTimeout(entry.timer);
      pending.clear();
    };
  }, [qc]);

  const patchTask = useCallback((pid: string, taskId: string, patch: Partial<GraphTaskNode>) => {
    qc.setQueryData<ProjectGraphResponse>(projectGraphKey(pid), (previous) => {
      if (!previous?.tasks?.some((task) => task.id === taskId)) return previous;
      return {
        ...previous,
        tasks: previous.tasks.map((task) => task.id === taskId ? { ...task, ...patch } : task),
      };
    });
  }, [qc]);

  // Keep the stream subscription stable while reading the latest project scope.
  const handlerRef = useRef<(event: NotifyEvent) => void>(() => {});
  handlerRef.current = (event) => {
    const type = event.event_type;
    const task = "task" in event ? event.task : undefined;
    const pid = event.project_id ?? task?.project_id;
    const ids = selectedRef.current;

    if (type === "task.blocked" || type === "task.unblocked") {
      if (pid && ids.includes(pid)) patchTask(pid, event.task_id, { is_blocked: type === "task.blocked" });
    } else if (
      type === "notify.task_started" || type === "notify.task_completed" ||
      type === "notify.task_failed" || type === "notify.task_stopped" || type === "notify.task_blocked"
    ) {
      // Replay frames may only carry task_id. Live notifications carry the
      // full Task shape, whose assignment field is called assigned_agent.
      if (task && pid && ids.includes(pid)) {
        patchTask(pid, task.id, { status: task.status, assigned_agent_id: task.assigned_agent ?? null });
      }
    }

    // session.* frames carry no graph content: an agent docking or leaving a
    // node arrives as agent.updated. Refetching the whole snapshot for every
    // session lifecycle frame multiplied the most expensive request the
    // dashboard makes (perf investigation 2026-09-04 §7).
    const graphEvent = /^(task|agent|gate)\./.test(type) || (
      type.startsWith("notify.") && !!task
    );
    if (!graphEvent) return;
    // Global workers can move between projects. Deletion/archive also removes
    // incoming cross-project edges, so their other selected snapshots refresh.
    const allProjects = type.startsWith("agent.") || type === "task.deleted" || type === "task.archived";
    refresh(!allProjects && pid ? ids.filter((id) => id === pid) : ids);
  };
  const onEvent = useCallback((event: NotifyEvent) => handlerRef.current(event), []);

  const previousStatusRef = useRef<ConnectionStatus | null>(null);
  const onStatusChange = useCallback((status: ConnectionStatus) => {
    const previous = previousStatusRef.current;
    previousStatusRef.current = status;
    // Not every event is persisted, and a server restart may reset replay.
    if (status === "connected" && previous !== null && previous !== "connected") {
      refresh(selectedRef.current);
    }
  }, [refresh]);

  useEventStream({ onEvent, onStatusChange });
}
