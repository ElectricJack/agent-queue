import { vi } from "vitest";

const sdk = vi.hoisted(() => ({
  recordCapabilities: vi.fn(), recordSearch: vi.fn(), knowledgeShow: vi.fn(), knowledgeHistory: vi.fn(),
  knowledgeDiff: vi.fn(), knowledgeUpdate: vi.fn(), recordShow: vi.fn(), linkList: vi.fn(),
  knowledgeCreate: vi.fn(), knowledgeCreateTask: vi.fn(), knowledgeRetire: vi.fn(), knowledgeRestore: vi.fn(),
  knowledgePropose: vi.fn(), linkCreate: vi.fn(),
}));
vi.mock("../../../api/client", () => sdk);
export { sdk };

export const recordId = "00000000-0000-0000-0000-000000000001";
export const revisionId = "00000000-0000-0000-0000-000000000002";
export const envelope = {
  success: true, record_id: recordId, knowledge_alias: "kn-example", revision_id: revisionId, sequence: 1,
  current_revision_id: revisionId, current_sequence: 1, scope_key: "project:p", created_at: "2026-10-01T00:00:00Z",
  actor_id: "local-operator", change_kind: "create", allowed_actions: ["history", "edit", "create_task"],
  snapshot: { title: "Live finding", body: "Selected evidence", category: "incident", lifecycle: "active", verification: "unverified",
    tags: [], sources: [], outgoing_links: [], summary: null },
};
export function resetSDK() {
  Object.values(sdk).forEach((mock) => mock.mockReset());
  sdk.recordCapabilities.mockResolvedValue({ data: { capabilities: { enabled: true, ui_enabled: true, writes_enabled: true,
    enabled_projects: ["p", "q"], legacy_memory_mode: "disabled", granted_operations: ["knowledge_create", "link_create"] } } });
  sdk.recordSearch.mockResolvedValue({ data: { items: [{ ...envelope, ...envelope.snapshot, title: "Live finding", updated_at: envelope.created_at }], next_cursor: null } });
  sdk.knowledgeShow.mockResolvedValue({ data: envelope });
  sdk.linkList.mockResolvedValue({ data: { links: [] } });
  sdk.recordShow.mockResolvedValue({ data: { kind: "task", link_token: "original-token", task: { id: "t", title: "Work" } } });
  sdk.knowledgeHistory.mockResolvedValue({ data: { revisions: [] } });
}
