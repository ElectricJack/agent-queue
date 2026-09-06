import { useEffect, useState } from "react";
import { skipToken, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { taskComment, taskCommentDelete, taskCommentEdit, taskComments } from "./client";

export const COMMENT_PAGE_SIZE = 50;
export const COMMENT_MAX_LENGTH = 16_000;
export const commentMutationKey = (taskId: string) => ["taskComment", taskId];
const draftKey = (taskId: string) => ["taskCommentDraft", taskId];

export function useTaskComments(taskId: string, offset: number) {
  return useQuery({
    // Existing task.updated invalidation refreshes every loaded comment page.
    queryKey: ["task", taskId, "comments", offset],
    queryFn: async () => (await taskComments({
      body: { task_id: taskId, limit: COMMENT_PAGE_SIZE, offset }, throwOnError: true,
    })).data,
    enabled: !!taskId,
    refetchInterval: 60_000,
  });
}

export function useTaskCommentDraft(taskId: string) {
  const client = useQueryClient();
  // Keep drafts task-scoped across drawer switches and loading/unmounts. They
  // are local cache data only, never part of a task fetch or WebSocket event.
  const { data: draft = "" } = useQuery({
    queryKey: draftKey(taskId), initialData: "", queryFn: skipToken,
    staleTime: Infinity, gcTime: Infinity,
  });
  // Controlled input state updates synchronously; cache notifications are
  // batched. Mirror cache changes so an in-flight submit also clears a draft
  // when this task was left and reopened before the response arrived.
  const [value, setValue] = useState(draft);
  useEffect(() => { setValue(draft); }, [draft]);
  return [value, (body: string) => {
    setValue(body);
    client.setQueryData(draftKey(taskId), body);
  }] as const;
}

export function useAddTaskComment(taskId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationKey: commentMutationKey(taskId),
    mutationFn: async (body: string) => (await taskComment({
      body: { task_id: taskId, body }, throwOnError: true,
    })).data,
    onSuccess: (_data, body) => {
      // Runs even if the user navigated away during the request. Only clear
      // the submitted draft; preserve any text typed while it was in flight.
      client.setQueryData(draftKey(taskId), (draft: string | undefined) => draft === body ? "" : draft);
      void client.invalidateQueries({ queryKey: ["task", taskId] });
      void client.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}

export function useEditTaskComment(taskId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationKey: commentMutationKey(taskId),
    mutationFn: async ({ commentId, body }: { commentId: string; body: string }) => (await taskCommentEdit({
      body: { task_id: taskId, comment_id: commentId, body }, throwOnError: true,
    })).data,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["task", taskId] });
    },
  });
}

export function useDeleteTaskComment(taskId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationKey: commentMutationKey(taskId),
    mutationFn: async (commentId: string) => (await taskCommentDelete({
      body: { task_id: taskId, comment_id: commentId }, throwOnError: true,
    })).data,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["task", taskId] });
    },
  });
}
