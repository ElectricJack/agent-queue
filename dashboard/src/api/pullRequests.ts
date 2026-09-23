import { useQuery } from "@tanstack/react-query";

import {
  client,
  getPendingPullRequestsApiReviewsPullRequestsGet,
  type PendingPullRequestsResponse,
} from "./client";

export function usePendingPullRequests() {
  return useQuery({
    queryKey: ["reviews", "pull-requests"],
    queryFn: async () => {
      const { data } = await getPendingPullRequestsApiReviewsPullRequestsGet({
        client,
        throwOnError: true,
      });
      return data as PendingPullRequestsResponse;
    },
    staleTime: 60_000,
    refetchInterval: 60_000,
  });
}
