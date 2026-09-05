import type { SubmissionError } from "./state";

/** Normalize whatever a `SubmitProject` rejected with into a `SubmissionError`. */
export function toSubmissionError(err: unknown): SubmissionError {
  if (typeof err === "object" && err !== null && typeof (err as { message?: unknown }).message === "string") {
    const e = err as { message: string; code?: unknown; fieldErrors?: unknown };
    const fieldErrors: Record<string, string> = {};
    if (typeof e.fieldErrors === "object" && e.fieldErrors !== null) {
      for (const [k, v] of Object.entries(e.fieldErrors as Record<string, unknown>)) {
        if (typeof v === "string") fieldErrors[k] = v;
      }
    }
    return { message: e.message, code: typeof e.code === "string" ? e.code : undefined, fieldErrors };
  }
  return { message: "Project creation failed.", fieldErrors: {} };
}
