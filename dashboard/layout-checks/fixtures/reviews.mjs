// The reviews inbox: what `/reviews` asks for beyond the shell's own requests.
import { REVIEW as COMMENT_REVIEW_ID, review as commentReview } from "./review-comments.mjs";

/** @satisfies {import("@aq/ts-client").PullRequestProjectSummariesResponse} */
const pullRequests = { projects: [] };

export const REVIEW_ID = "fixture-review";
export const REVIEW_TITLE = "../Café\\plan: draft?";
export const REVIEW_MARKDOWN = {
  1: "---\r\nstatus: draft\r\n---\r\n# Original plan\r\n\r\n## Goal\r\n\r\nHistorical café 日本語 🐈  \r\n\r\n",
  2: "---\nstatus: revised\n---\n# Current plan\n\n## Goal\n\nLatest **Markdown**, [link](https://example.test).\n",
};

/**
 * @param {import("@aq/ts-client").ReviewShowRequest} body
 * @returns {import("@aq/ts-client").ReviewShowResponse}
 */
function review(body) {
  if (body.review_id === COMMENT_REVIEW_ID) return commentReview;
  const revision = body.revision === 1 ? 1 : 2;
  return {
    success: true,
    review: {
      id: REVIEW_ID, project_id: "fixture", title: REVIEW_TITLE, kind: "plan",
      state: "in_review", current_revision: 2, decider: "user",
      vault_path: "projects/fixture/plans/download.md",
    },
    revision: { revision, content: REVIEW_MARKDOWN[revision] },
    revisions: [{ revision: 1 }, { revision: 2 }],
    vault_state: "ok", comments: [], attachments: [],
    diff: body.diff_from != null ? [{ op: "added", text: "Diff text is not the document" }] : [],
    response_route: { kind: "new_task", summary: "Approve sends the decision to the supervisor.", class_summaries: {} },
  };
}

/** @param {import("@aq/ts-client").AskShowRequest} body */
function ask(body) {
  const source = body.alias_kind ? body.ask_id : body.ask_id.replace(/^ask-/, "");
  const document = review({ review_id: source });
  const version = { decision_generation: 1, source_fingerprint: "fixture", feedback_version: 0 };
  return { success: true, ask: { ...version, id: `ask-${source}`, source_kind: "review", source_key: source,
    project_id: "fixture", severity: "normal", next_actor: "human", context_state: "complete",
    application_state: "not_started", decision_state: "pending", context: { title: document.review.title, why: "Review this proposal" } },
    expected: version, actions: [{ action: "discuss", enabled: true }], inputs: [], history: [],
    obligations: [], operations: [], source_health: "healthy", document: { revision: 2 } };
}

export const routes = {
  "POST /api/review/ask-show": ask,
  "GET /api/reviews/pull-requests/summary": () => pullRequests,
  "POST /api/review/show": review,
};
