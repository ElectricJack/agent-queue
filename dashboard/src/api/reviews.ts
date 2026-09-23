import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import {
  reviewComment,
  reviewDecide,
  reviewImportEdits,
  reviewList,
  reviewShow,
  type ReviewCommentResponse,
  type ReviewDecideResponse,
  type ReviewImportEditsResponse,
  type ReviewListResponse,
  type ReviewShowResponse,
} from "./client";

type ReviewFilters = { projectId?: string; state?: string; kind?: string; taskId?: string };

export function useReviews(filters: ReviewFilters): UseQueryResult<ReviewListResponse> {
  return useQuery({
    queryKey: ["reviews", filters],
    queryFn: async () => (await reviewList({
      body: {
        ...(filters.projectId ? { project_id: filters.projectId } : {}),
        ...(filters.state ? { state: filters.state } : {}),
        ...(filters.kind ? { kind: filters.kind } : {}),
        ...(filters.taskId ? { task_id: filters.taskId } : {}),
      },
      throwOnError: true,
    })).data as ReviewListResponse,
  });
}

export function useReview(
  reviewId: string,
  opts: { revision?: number; diffFrom?: number } = {},
): UseQueryResult<ReviewShowResponse> {
  return useQuery({
    queryKey: ["review", reviewId, opts.revision ?? "current", opts.diffFrom ?? null],
    queryFn: async () => (await reviewShow({
      body: {
        review_id: reviewId,
        comments: true,
        ...(opts.revision != null ? { revision: opts.revision } : {}),
        ...(opts.diffFrom != null ? { diff_from: opts.diffFrom } : {}),
      },
      throwOnError: true,
    })).data as ReviewShowResponse,
    enabled: Boolean(reviewId),
  });
}

/** Count reviews awaiting a local-operator decision for the rail badge. */
export function useWaitingReviewCount(): number {
  const { data } = useReviews({ state: "in_review" });
  return (data?.reviews ?? []).filter(
    (review) => review.decider === "user" || review.decider === "user_or_supervisor",
  ).length;
}

type DecideInput = {
  review_id: string;
  revision: number;
  decision: "approve" | "request_changes";
  note?: string;
  responder_class?: string;
  responder_profile?: string;
};

export function useDecideReview(): UseMutationResult<ReviewDecideResponse, Error, DecideInput> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: DecideInput) => (await reviewDecide({
      body: input,
      throwOnError: true,
    })).data as ReviewDecideResponse,
    onSuccess: (_data, input) => {
      void queryClient.invalidateQueries({ queryKey: ["reviews"] });
      void queryClient.invalidateQueries({ queryKey: ["review", input.review_id] });
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      void queryClient.invalidateQueries({ queryKey: ["gates"] });
    },
  });
}

type CommentInput = {
  review_id: string;
  revision: number;
  quote?: string | null;
  heading_path?: string[];
  body: string;
};

export function useCommentReview(): UseMutationResult<ReviewCommentResponse, Error, CommentInput> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: CommentInput) => (await reviewComment({
      body: input,
      throwOnError: true,
    })).data as ReviewCommentResponse,
    onSuccess: (_data, input) => {
      void queryClient.invalidateQueries({ queryKey: ["reviews"] });
      void queryClient.invalidateQueries({ queryKey: ["review", input.review_id] });
    },
  });
}

type ImportEditsInput = { review_id: string };

export function useImportReviewEdits(): UseMutationResult<
  ReviewImportEditsResponse,
  Error,
  ImportEditsInput
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: ImportEditsInput) => (await reviewImportEdits({
      body: input,
      throwOnError: true,
    })).data as ReviewImportEditsResponse,
    onSuccess: (_data, input) => {
      void queryClient.invalidateQueries({ queryKey: ["reviews"] });
      void queryClient.invalidateQueries({ queryKey: ["review", input.review_id] });
    },
  });
}
