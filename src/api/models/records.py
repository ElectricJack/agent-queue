"""Response models for the record read / link / capability commands (K03).

Record reads (``record_show``, ``link_list``) are flat envelopes like the
knowledge commands; ``record_search`` is lexically paginated and
``record_capabilities`` is a pure config read, so each carries its own shape.
The models stay permissive (``extra="allow"``) over the service's internal
fields and only pin the stable envelope.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RecordEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")
    success: bool = True
    outcome: str | None = None
    record_id: str | None = None
    replay: bool = False


class RecordShowResponse(RecordEnvelope):
    kind: str | None = None
    revision_id: str | None = None
    sequence: int = 0
    snapshot: dict = {}


class RecordSearchResponse(RecordEnvelope):
    items: list[dict] = []
    count: int = 0
    next_cursor: str | None = None


class RecordCapabilitiesResponse(RecordEnvelope):
    capabilities: dict = {}


class LinkListResponse(RecordEnvelope):
    links: list[dict] = []


class LinkMutationResponse(RecordEnvelope):
    links: list[dict] = []


class RecordRepairResponse(RecordEnvelope):
    dry_run: bool = True
    inventory: dict | None = None
    batches: list[dict] | None = None
    done: bool | None = None
    eligible: bool | None = None


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "record_show": RecordShowResponse,
    "record_search": RecordSearchResponse,
    "record_capabilities": RecordCapabilitiesResponse,
    "link_create": LinkMutationResponse,
    "link_list": LinkListResponse,
    "link_remove": LinkMutationResponse,
    "record_repair": RecordRepairResponse,
}
