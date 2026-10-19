import type { RefObject } from "react";
import type { AskDetailData } from "../../api/asks";
import { AskDecision } from "../../components/asks/AskDecision";
export type ResponseRoute = { kind: string; summary: string; class_summaries: Record<string, string> };
export function DecisionBar({ data, disabledReason, ...drafts }: {
  approveRef?: RefObject<HTMLButtonElement | null>; responseRoute?: ResponseRoute;
  data: AskDetailData; disabledReason: string | null; draftBlocked: boolean;
  pendingSave?: boolean; saveDraft: () => Promise<void>;
}) {
  const guarded = disabledReason ? { ...data, actions: data.actions.map(action =>
    action.action === "decide" || action.action === "prepare_decision"
      ? { ...action, enabled: false, disabled_reason: disabledReason } : action) } : data;
  return <div className="border-t border-gray-800 bg-gray-950/95">
    <AskDecision data={guarded} {...drafts} />
  </div>;
}
