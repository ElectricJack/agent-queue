// Digest schedule health, dry preview and the escalation inbox.
//
// Every call is a named daemon command through the generated client — the
// dashboard owns no messaging business logic of its own (spec §9): eligibility,
// rendering, suppression reasons and reply authorisation all come back from the
// server exactly as Discord would see them.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  digestPreview,
  digestStatus,
  escalationGet,
  escalationList,
  escalationReply,
  type DigestPreviewResponse,
  type DigestStatusResponse,
  type EscalationGetResponse,
  type EscalationListResponse,
  type EscalationReplyResponse,
} from "./client";

export type {
  DigestPreviewResponse,
  DigestStatusResponse,
  EscalationGetResponse,
  EscalationListResponse,
};

/** Configured destination, next evaluation and delivery health. */
export function useDigestStatus() {
  return useQuery({
    queryKey: ["digest-status"],
    queryFn: async () => {
      const { data } = await digestStatus({ body: {}, throwOnError: true });
      return data as DigestStatusResponse;
    },
    refetchInterval: 30_000,
  });
}

/**
 * Dry preview of the current window.
 *
 * `enabled` is the caller's, not a default: a preview is an explicit operator
 * action. It still sends nothing — the command reserves no window and moves no
 * cursor — so re-running it cannot consume the next real digest.
 */
export function useDigestPreview(enabled: boolean) {
  return useQuery({
    queryKey: ["digest-preview"],
    queryFn: async () => {
      const { data } = await digestPreview({ body: {}, throwOnError: true });
      return data as DigestPreviewResponse;
    },
    enabled,
    staleTime: 0,
    gcTime: 0,
  });
}

export function useEscalations(projectId?: string) {
  return useQuery({
    queryKey: ["escalations", projectId ?? "all"],
    queryFn: async () => {
      const { data } = await escalationList({
        body: projectId ? { project_id: projectId } : {},
        throwOnError: true,
      });
      return data as EscalationListResponse;
    },
    refetchInterval: 20_000,
  });
}

export function useEscalation(escalationId: string | null) {
  return useQuery({
    queryKey: ["escalation", escalationId],
    queryFn: async () => {
      const { data } = await escalationGet({
        body: { escalation_id: escalationId as string },
        throwOnError: true,
      });
      return data as EscalationGetResponse;
    },
    enabled: Boolean(escalationId),
  });
}

/**
 * A human reply. The dashboard never mutates a task from here: the reply is
 * persisted and handed to the owning supervisor, which decides what to do.
 */
export function useEscalationReply() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: { escalation_id: string; text: string; external_message_id: string }) =>
      (await escalationReply({ body: input, throwOnError: true })).data as EscalationReplyResponse,
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ["escalation", variables.escalation_id] });
      queryClient.invalidateQueries({ queryKey: ["escalations"] });
    },
  });
}
