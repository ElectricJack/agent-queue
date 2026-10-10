import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import {
  reviewComment,
  reviewAttachmentAdd,
  reviewDecide,
  reviewImportEdits,
  reviewList,
  reviewShow,
  reviewWithdraw,
  reviewReopen,
  type ReviewCommentResponse,
  type ReviewDecideResponse,
  type ReviewImportEditsResponse,
  type ReviewListResponse,
  type ReviewShowResponse,
  type ReviewWithdrawResponse,
  type ReviewSubmitResponse,
} from "./client";

export type ReviewAttachment = {
  id: string;
  review_id: string;
  revision: number;
  url: string;
  sha256: string;
  content_type: string;
  size: number;
  caption: string;
  view_id: string;
  candidate_id: string;
};

type AttachInput = {
  review_id: string;
  revision: number;
  data_base64: string;
  content_type: string;
  caption: string;
  view_id: string;
  candidate_id: string;
};

export function useAttachReviewImage(): UseMutationResult<unknown, Error, AttachInput> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input) => (await reviewAttachmentAdd({
      body: input,
      throwOnError: true,
    })).data,
    onSuccess: (_data, input) => {
      void queryClient.invalidateQueries({ queryKey: ["review", input.review_id] });
    },
  });
}

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
  decision: "approve" | "request_changes" | "reject";
  note?: string;
  responder_class?: string;
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

type WithdrawInput = { review_id: string; reason: string };

export function useReopenReview(): UseMutationResult<
  ReviewSubmitResponse, Error, { review_id: string; revision: number }
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input) => (await reviewReopen({
      body: input, throwOnError: true,
    })).data as ReviewSubmitResponse,
    onSuccess: (_data, input) => {
      void queryClient.invalidateQueries({ queryKey: ["reviews"] });
      void queryClient.invalidateQueries({ queryKey: ["review", input.review_id] });
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      void queryClient.invalidateQueries({ queryKey: ["gates"] });
    },
  });
}

/** Close an open review with no decision, exactly as `aq review withdraw` does. */
export function useWithdrawReview(): UseMutationResult<
  ReviewWithdrawResponse,
  Error,
  WithdrawInput
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: WithdrawInput) => (await reviewWithdraw({
      body: input,
      throwOnError: true,
    })).data as ReviewWithdrawResponse,
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
