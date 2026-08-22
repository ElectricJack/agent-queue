import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { ProjectGraphResponse } from "@aq/ts-client";
import { useEventStream } from "../../ws/useEventStream";
import type { NotifyEvent } from "../../ws/types";
import { projectGraphKey } from "../../api/graph";
import type { GraphTaskNode } from "./types";

type Snapshot = ProjectGraphResponse;

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

  const onEvent = useCallback(
    (ev: NotifyEvent) => {
      const type = ev.event_type;

      // task.blocked / task.unblocked (work-graph events) — restyle only.
      if (type === "task.blocked" || type === "task.unblocked") {
        const pid = (ev as { project_id?: string }).project_id;
        const tid = (ev as { task_id?: string }).task_id;
        if (!pid || !tid || !projectIds.includes(pid)) return;
        patchTask(pid, tid, { is_blocked: type === "task.blocked" });
        return;
      }

      // Task lifecycle events carry the full Task shape.
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

      // Gate events: prefer targeted refetch per project (project_id not always
      // on gate frames; cheapest safe fallback is per-project invalidate).
      if (
        type === "gate.created" ||
        type === "gate.resolved" ||
        type === "gate.expired"
      ) {
        for (const pid of projectIds) {
          qc.invalidateQueries({ queryKey: projectGraphKey(pid), exact: true });
        }
        return;
      }

      // Session lifecycle: reflect agent presence next to a task.
      if (
        type === "session.started" ||
        type === "session.exited" ||
        type === "session.adopted"
      ) {
        const pid = (ev as { project_id?: string }).project_id;
        if (!pid || !projectIds.includes(pid)) return;
        qc.invalidateQueries({ queryKey: projectGraphKey(pid), exact: true });
        return;
      }

      // task.created is not in the current WS union — the 60 s reconciliation
      // refetch plus any subsequent task.* event will surface the row.
    },
    [patchTask, projectIds, qc],
  );

  useEventStream({ onEvent });
}
