"""Shared opt-in paging fields for existing typed contracts and API DTOs."""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_serializer


class SnapshotArgs(BaseModel):
    pagination: Literal["snapshot"] | None = None
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=128)
    page_token: str | None = Field(default=None, min_length=1, max_length=128)
    page_size: int | None = Field(default=None, ge=1, le=500)


class SnapshotValue(BaseModel):
    success: bool = True
    complete: bool | None = None
    execution_status: str | None = None
    error: str | None = None
    error_code: str | None = None
    reasons: list[str] = Field(default_factory=list)
    snapshot_id: str | None = None
    producer: str | None = None
    producer_version: int | None = None
    observed_at: float | None = None
    materialized_at: float | None = None
    expires_at: float | None = None
    interval_started_at: float | None = None
    interval_finished_at: float | None = None
    coverage: list[dict[str, Any]] = Field(default_factory=list)
    identity_manifest: list[dict[str, Any]] = Field(default_factory=list)
    page_tokens: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    page_count: int = 0
    page_kind: str | None = None
    page_index: int | None = None

    @model_serializer(mode="wrap")
    def serialize_snapshot(self, handler, info):
        result = handler(self)
        if self.complete is not None and info.include is None and info.exclude is None:
            # API row DTOs have many defaults for live detail views. Expanding
            # them once per row would inflate an already bounded snapshot page.
            # Keep authored fields recursively; leave legacy responses alone.
            for name in type(self).model_fields:
                value = getattr(self, name)
                if (name in result and isinstance(value, list)
                        and any(isinstance(item, BaseModel) for item in value)):
                    result[name] = [item.model_dump(mode=info.mode, exclude_unset=True,
                                                   exclude_none=info.exclude_none)
                                    if isinstance(item, BaseModel) else item for item in value]
        return result
