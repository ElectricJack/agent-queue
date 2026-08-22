import { useCallback, useRef } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import type { ProjectGraphResponse } from "@aq/ts-client";
import { useEventStream } from "../../ws/useEventStream";
import type { NotifyEvent } from "../../ws/types";
import { projectGraphKey } from "../../api/graph";
import type { GraphTaskNode } from "./types";

type Snapshot = ProjectGraphResponse;

/** At most one invalidate per project per this window when a gate/session
 *  frame doesn't carry a project_id and we have to fall back to refreshing
 *  every selected project. */
const FALLBACK_DEBOUNCE_MS = 500;

/** Incrementally mutate cached per-project graph snapshots on WS events. */
export function useGraphLive(projectIds: string[]) {
  const qc = useQueryClient();

  const patchTask = useCallback(
    (pid: string, taskId: string, patch: Partial<GraphTaskNode>) => {
      qc.setQueryData<Snapshot>(projectGraphKey(pid), (prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          tasks: (prev.tasks ?? []).map((t) =>
            t.id === taskId ? { ...t, ...patch } : t,
          ),
        };
      });
    },
    [qc],
  );

  // Debounced fallback invalidation, keyed by project id, for event families
  // that don't reliably carry project_id on every frame.
  const lastFallbackInvalidateRef = useRef<Map<string, number>>(new Map());
  const invalidateAllDebounced = useCallback(
    (client: QueryClient, ids: string[]) => {
      const now = Date.now();
      const last = lastFallbackInvalidateRef.current;
      for (const pid of ids) {
        const prevTs = last.get(pid) ?? 0;
        if (now - prevTs < FALLBACK_DEBOUNCE_MS) continue;
        last.set(pid, now);
        client.invalidateQueries({ queryKey: projectGraphKey(pid), exact: true });
      }
    },
    [],
  );

  // `onEvent` is passed to useEventStream, whose subscription effect keys off
  // its identity. `projectIds` (a fresh array from the project-strip selector)
  // changes often, so we keep the callback identity handed to useEventStream
  // stable and read the live `projectIds`/`qc`/helpers through a ref instead —
  // avoids re-running that subscription effect on every project toggle.
  const handlerRef = useRef<(ev: NotifyEvent) => void>(() => {});
  handlerRef.current = (ev: NotifyEvent) => {
    const type = ev.event_type;

    // task.blocked / task.unblocked (work-graph events) — restyle only.
    if (type === "task.blocked" || type === "task.unblocked") {
      const pid = (ev as { project_id?: string }).project_id;
      const tid = (ev as { task_id?: string }).task_id;
      if (!pid || !tid || !projectIds.includes(pid)) return;
      patchTask(pid, tid, { is_blocked: type === "task.blocked" });
      return;
    }

    // Task lifecycle events carry the full Task shape (api/hooks.ts `Task`,
    // i.e. GetTaskResponse — field is `assigned_agent`), which we map onto
    // the merged graph's GraphTaskNode field, `assigned_agent_id`. These
    // two names are NOT the same string on purpose — do not "fix" one to
    // match the other without checking both type defs.
    if (
      type === "notify.task_started" ||
      type === "notify.task_completed" ||
      type === "notify.task_failed" ||
      type === "notify.task_stopped" ||
      type === "notify.task_blocked"
    ) {
      const t = (
        ev as {
          task?: {
            id: string;
            project_id: string;
            status: string;
            assigned_agent?: string | null;
          };
        }
      ).task;
      if (!t || !projectIds.includes(t.project_id)) return;
      patchTask(t.project_id, t.id, {
        status: t.status,
        assigned_agent_id: t.assigned_agent ?? null,
      });
      return;
    }

    // Gate events: project_id is present on most frames — invalidate just
    // that project's graph. When it's missing, fall back to invalidating
    // every selected project, debounced to at most once per project per
    // FALLBACK_DEBOUNCE_MS so a chatty gate stream can't trigger N full
    // graph refetches per event.
    if (
      type === "gate.created" ||
      type === "gate.resolved" ||
      type === "gate.expired"
    ) {
      const pid = (ev as { project_id?: string | null }).project_id;
      if (pid) {
        if (projectIds.includes(pid)) {
          qc.invalidateQueries({ queryKey: projectGraphKey(pid), exact: true });
        }
        return;
      }
      invalidateAllDebounced(qc, projectIds);
      return;
    }

    // Session lifecycle: reflect agent presence next to a task. Same
    // project_id-first-else-debounced-fallback strategy as gate events.
    if (
      type === "session.started" ||
      type === "session.exited" ||
      type === "session.adopted"
    ) {
      const pid = (ev as { project_id?: string | null }).project_id;
      if (pid) {
        if (projectIds.includes(pid)) {
          qc.invalidateQueries({ queryKey: projectGraphKey(pid), exact: true });
        }
        return;
      }
      invalidateAllDebounced(qc, projectIds);
      return;
    }

    // task.created is not in the current WS union — the 60 s reconciliation
    // refetch plus any subsequent task.* event will surface the row.
  };

  const onEvent = useCallback((ev: NotifyEvent) => {
    handlerRef.current(ev);
  }, []);

  useEventStream({ onEvent });
}
