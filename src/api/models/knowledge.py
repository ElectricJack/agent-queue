"""Response models for the knowledge record commands (K03).

Reads and writes return the same flat envelopes (``success`` / ``outcome`` /
``replay``) that the record service produces; these models only pin the shape
the dashboard and MCP surfaces may depend on, and stay permissive about the
service's internal fields.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class KnowledgeEnvelope(BaseModel):
    """Common top-level fields every knowledge command result carries."""

    model_config = ConfigDict(extra="allow")
    success: bool = True
    outcome: str | None = None
    record_id: str | None = None
    replay: bool = False
    content_sha256: str | None = None
    revision_id: str | None = None


class KnowledgeCreateResponse(KnowledgeEnvelope):
    kind: str | None = None
    version: int | None = None


class KnowledgeCreateTaskResponse(KnowledgeEnvelope):
    task_id: str
    task_record_id: str
    link_id: str
    route_source: str
    status: str
    gate_ids: list[str] = []
    parent_id: str | None = None


class KnowledgeListResponse(KnowledgeEnvelope):
    items: list[dict] = []
    count: int = 0
    next_cursor: str | None = None


class KnowledgeShowResponse(KnowledgeEnvelope):
    kind: str = "knowledge"
    knowledge_alias: str | None = None
    sequence: int = 0
    content_sha256: str | None = None
    hash_version: int = 1
    allowed_actions: list[str] = []
    protection: str = "none"
    current_revision_id: str
    current_sequence: int
    scope_key: str
    created_at: str
    actor_id: str
    change_kind: str
    stale: bool = False
    stale_reason: str | None = None
    authority: dict | None = None
    snapshot: dict = {}


class KnowledgeUpdateResponse(KnowledgeEnvelope):
    version: int | None = None


class KnowledgeHistoryResponse(KnowledgeEnvelope):
    revisions: list[dict] = []


class KnowledgeDiffResponse(KnowledgeEnvelope):
    record_id: str | None = None
    from_revision: str | None = None
    to_revision: str | None = None
    changes: list[dict] = []


class KnowledgeLifecycleResponse(KnowledgeEnvelope):
    version: int | None = None


class KnowledgeExportResponse(KnowledgeEnvelope):
    content: str = ""
    export_sha256: str | None = None
    format_version: int = 1


class KnowledgeImportResponse(KnowledgeEnvelope):
    """The sealed, read-only inventory report returned by the local operator scan."""

    source_installation_id: str
    snapshot_id: str
    snapshot_timestamp: str
    manifest_sha256: str
    manifest_content_base64: str
    vector_observation: str
    counts: dict
    items: list[dict]
    mappings: list[dict]
    identities: list[dict]


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "knowledge_create": KnowledgeCreateResponse,
    "knowledge_create_task": KnowledgeCreateTaskResponse,
    "knowledge_list": KnowledgeListResponse,
    "knowledge_show": KnowledgeShowResponse,
    "knowledge_update": KnowledgeUpdateResponse,
    "knowledge_history": KnowledgeHistoryResponse,
    "knowledge_diff": KnowledgeDiffResponse,
    "knowledge_retire": KnowledgeLifecycleResponse,
    "knowledge_restore": KnowledgeLifecycleResponse,
    "knowledge_export": KnowledgeExportResponse,
    "knowledge_import": KnowledgeImportResponse,
}


class KnowledgeProtectionResponse(KnowledgeEnvelope):
    proposal_id: str | None = None
    proposal_sha256: str | None = None
    state: str | None = None
    snapshot: dict | None = None
    authority: dict | None = None
    redaction_id: str | None = None
    cleanup_state: dict | None = None
    dry_run: bool | None = None


from src.commands.contracts.knowledge_protection import PROTECTION_COMMANDS  # noqa: E402

RESPONSE_MODELS.update({name: KnowledgeProtectionResponse for name, *_ in PROTECTION_COMMANDS})
