import type { RecordFilters, RecordKindFilter } from "../../api/records";
import { readKnowledgeFilters, writeKnowledgeFilters } from "../knowledge/knowledgeUrlState";

export type RecordSelection = { kind: "task" | "knowledge"; recordId: string; revisionId: string | null };
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
  const kind = params.get("recordKind");
  const recordId = params.get("record");
  if (!recordId || (kind !== "knowledge" && kind !== "task")) return null;
  return { kind, recordId, revisionId: kind === "knowledge" ? params.get("revision") || null : null };
}
export function writeRecordSelection(params: URLSearchParams, selection: RecordSelection | null) {
  const next = new URLSearchParams(params);
  for (const key of ["record", "recordKind", "revision"]) next.delete(key);
  if (selection) {
    next.set("record", selection.recordId);
    next.set("recordKind", selection.kind);
    if (selection.kind === "knowledge" && selection.revisionId) next.set("revision", selection.revisionId);
  }
  return next;
}
