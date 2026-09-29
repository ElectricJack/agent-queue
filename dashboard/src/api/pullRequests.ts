import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  client,
  approvePullRequestApiReviewsPullRequestsTaskIdApprovePost,
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

export function useApprovePullRequest() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ taskId, headSha }: { taskId: string; headSha: string }) => {
      const { data } = await approvePullRequestApiReviewsPullRequestsTaskIdApprovePost({
        client, throwOnError: true,
        path: { task_id: taskId }, body: { head_sha: headSha },
      });
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["reviews", "pull-requests"] }),
  });
}
