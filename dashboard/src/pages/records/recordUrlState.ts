import type { RecordFilters, RecordKindFilter } from "../../api/records";
import { readKnowledgeFilters, writeKnowledgeFilters } from "../knowledge/knowledgeUrlState";

export type RecordSelection = { kind: "task" | "knowledge"; recordId: string; revisionId: string | null; taskId?: string };
export function readRecordFilters(params: URLSearchParams): RecordFilters {
  const kind = params.get("kind");
  return { ...readKnowledgeFilters(params), kind: kind === "task" || kind === "knowledge" ? kind : "all" };
}
export function writeRecordFilters(params: URLSearchParams, filters: RecordFilters) {
  const next = writeKnowledgeFilters(params, filters);
  if (filters.kind === "all") next.delete("kind");
  else next.set("kind", filters.kind satisfies RecordKindFilter);
  return next;
}
export function readRecordSelection(params: URLSearchParams): RecordSelection | null {
  const taskId = params.get("task");
  if (taskId) return { kind: "task", recordId: taskId, taskId, revisionId: null };
  const kind = params.get("recordKind");
  const recordId = params.get("record");
  if (!recordId || (kind !== "knowledge" && kind !== "task")) return null;
  return { kind, recordId, revisionId: kind === "knowledge" ? params.get("revision") || null : null };
}
export function writeRecordSelection(params: URLSearchParams, selection: RecordSelection | null) {
  const next = new URLSearchParams(params);
  for (const key of ["record", "recordKind", "revision", "task"]) next.delete(key);
  if (selection) {
    if (selection.kind === "task" && selection.taskId) {
      next.set("task", selection.taskId);
      return next;
    }
    next.set("record", selection.recordId);
    next.set("recordKind", selection.kind);
    if (selection.kind === "knowledge" && selection.revisionId) next.set("revision", selection.revisionId);
  }
  return next;
}

/** Links that can address a task before it has a knowledge record identity. */
export function taskSelectionHref(projectId: string | null | undefined, taskId: string, search = "") {
  const params = writeRecordSelection(new URLSearchParams(search), {
    kind: "task", recordId: taskId, taskId, revisionId: null,
  });
  const base = projectId ? `/projects/${encodeURIComponent(projectId)}` : "/command-center";
  return `${base}/tasks-knowledge?${params}`;
}

export function knowledgeSelectionHref(projectId: string, recordId: string, revisionId: string | null = null) {
  const params = writeRecordSelection(new URLSearchParams(), { kind: "knowledge", recordId, revisionId });
  return `/projects/${encodeURIComponent(projectId)}/tasks-knowledge?${params}`;
}
