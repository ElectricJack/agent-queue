// The reviews inbox: what `/reviews` asks for beyond the shell's own requests.

/** @satisfies {import("@aq/ts-client").PendingPullRequestsResponse} */
const pullRequests = { pull_requests: [] };

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

export const routes = {
  "GET /api/reviews/pull-requests": () => pullRequests,
  "POST /api/review/show": review,
};
