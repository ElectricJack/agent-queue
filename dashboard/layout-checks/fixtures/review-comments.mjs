export const REVIEW = "fixture-long-review";
export const SECTION = "Section 18";
export const QUOTE = "The referenced paragraph must stay visible while writing a comment.";

const content = "# Long review\n\n" + Array.from({ length: 30 }, (_, index) =>
  `## Section ${index + 1}\n\n` +
  (index === 17 ? `### Details\n\n${QUOTE}\n\n` : "") +
  Array.from({ length: 5 }, () => "A long review contains enough detail to require scrolling far below the document title.").join("\n\n"),
).join("\n\n");

/** @satisfies {import("@aq/ts-client").ReviewShowResponse} */
export const review = {
  success: true,
  review: {
    id: REVIEW, project_id: "fixture", title: "Long review", kind: "spec", state: "in_review",
    current_revision: 2, decider: "user", vault_path: "specs/long-review.md",
  },
  revision: { revision: 2, content },
  revisions: [{ revision: 1 }, { revision: 2 }],
  vault_state: "ok",
  comments: [],
  attachments: [],
  response_route: { kind: "new_task", summary: "Request changes creates a revision task." },
};

export const routes = {
  "POST /api/review/comment": () => ({ success: true, comment_id: "fixture-comment" }),
};
