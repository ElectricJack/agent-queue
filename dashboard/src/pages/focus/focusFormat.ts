// Pure display helpers for the focus pages, kept out of the page components so
// the fast-refresh lint stays quiet and each rule is unit-testable.
import type { EpicDeliveryRef, EpicDeliveryStatus } from "@aq/ts-client";

/**
 * `choices` is a JSON list of strings on the wire; a mapping or a bare string
 * is what the daemon tolerates (`escalations/facts.py`), so a phone renders
 * every shape rather than `[object Object]`.
 */
export function choiceLabels(raw: unknown): string[] {
  if (typeof raw === "string") return [raw];
  if (Array.isArray(raw)) {
    return raw.map((choice) => (typeof choice === "string" ? choice : JSON.stringify(choice)));
  }
  if (raw && typeof raw === "object") {
    return Object.values(raw as Record<string, unknown>).map(String);
  }
  return [];
}

/** A delivery ref that names this integration batch. */
export function isBatchRef(ref: EpicDeliveryRef | null | undefined, batchId: string): boolean {
  return ref?.kind === "batch" && ref.id === batchId;
}

/** Every ref a delivery carries: the responsible party plus its links. */
export function deliveryRefs(delivery: EpicDeliveryStatus): EpicDeliveryRef[] {
  return [delivery.responsible, ...(delivery.links ?? [])].filter(
    (ref): ref is EpicDeliveryRef => !!ref,
  );
}

/** Whether an epic's delivery status points at this batch at all. */
export function referencesBatch(
  delivery: EpicDeliveryStatus | null | undefined,
  batchId: string,
): boolean {
  return !!delivery && deliveryRefs(delivery).some((ref) => isBatchRef(ref, batchId));
}

/** Epoch seconds as a local timestamp; absent evidence reads as a dash. */
export function when(epoch: number | null | undefined): string {
  return epoch ? new Date(epoch * 1000).toLocaleString() : "—";
}