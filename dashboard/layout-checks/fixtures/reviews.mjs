// The reviews inbox: what `/reviews` asks for beyond the shell's own requests.

/** @satisfies {import("@aq/ts-client").PendingPullRequestsResponse} */
const pullRequests = { pull_requests: [] };

export const routes = {
  "GET /api/reviews/pull-requests": () => pullRequests,
};
