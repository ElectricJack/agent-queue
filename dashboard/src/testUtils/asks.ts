import type { AskDetailData } from "../api/asks";
import type { AskPreview } from "../api/client";
export function askData(): AskDetailData {
  const version = { decision_generation: 2, source_fingerprint: "source-hash", feedback_version: 3 };
  return {
    success: true, ask: { ...version, id: "ask-one", project_id: "p", source_kind: "review", source_key: "rev-x",
      display_version: 1, created_at: 100, updated_at: 100, severity: "high", next_actor: "human",
      context_state: "complete", application_state: "not_started", decision_state: "pending",
      context: { title: "Architecture proposal", why: "Choose the design", waiting_effect: "Implementation waits for this decision" } },
    expected: version, actions: [{ action: "discuss", enabled: true }, ...["approve", "request_changes", "decline"].map(key =>
      ({ action: "decide", option_key: key, label: key === "approve" ? "Approve" : key === "decline" ? "Decline" : "Request changes", enabled: true }))],
    inputs: [], history: [], obligations: [], operations: [], source_health: "healthy", document: { revision: 2 },
  };
}
export function preview(): AskPreview {
  return { id: "preview-one", packet_sha256: "packet-sha", expires_at: Date.now() / 1000 + 300,
    packet: { context: { choices: [{ key: "approve", effect: "Approve and file follow-up" }] }, intent: { option_key: "approve", overall_note: "Accepted" },
      domain: { review_revision: 2, content_sha256: "content-sha", effective_option: "approve", feedback: [
        { feedback_key: "comment:one", original_text: "Document the restart case", original_revision: 2,
          original_author: "human:operator", disposition: "follow_up", acceptance_text: "Add restart coverage", required_before_release: true },
      ] } } };
}
export function askPage() { return { items: [askData().ask], count: 1, snapshot_at: 100, next_cursor: null, identity_hash: "set", source_health: "healthy" }; }
