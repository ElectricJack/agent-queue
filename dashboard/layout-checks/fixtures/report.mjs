import { CHILD_TASK_ID, LONG_TITLE, NOW } from "./base.mjs";

export const REPORT = "morning-2026-09-25";

/** @type {import("@aq/ts-client").ReportGetResponse} */
const report = {
  report: {
    id: REPORT, state: "final", reason: null, local_date: "2026-09-25", timezone: "UTC",
    planned_at: NOW, window_start: NOW - 86_400, window_end: NOW, brief_hash: null, created_at: NOW,
    finalized_at: NOW, author_deadline: NOW, is_fallback: false,
    report: {
      version: 1, summary: `${LONG_TITLE} ${"unbroken".repeat(40)}`,
      coverage: { complete: true },
      projects: [{
        id: "fixture", name: "Fixture project",
        landed: [{ refs: ["completion:c1", "x".repeat(200)], text: LONG_TITLE, task_id: CHILD_TASK_ID }],
        pending: [], failures: [], manual_checks: [],
      }],
    },
  },
};

export const routes = {
  "POST /api/report/get": /** @param {{report_id: string}} body */ (body) => (body.report_id === REPORT ? report : { __status: 404, body: { detail: "report not found" } }),
};
