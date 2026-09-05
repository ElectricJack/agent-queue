"""Frozen value objects shared by integration playbooks and core commands."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class BranchKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository_id: str
    branch: str


class Fence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target: BranchKey
    owner_id: str
    token: int


class PromotionInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_key: str
    source_task_id: str
    source_head: str
    source_base: str
    expected_target: str
    fence: Fence


class PromotionValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_id: str
    receipt_id: str | None = None
    prepared_sha: str | None = None


class RequiredCheckSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    names: tuple[str, ...] = Field(min_length=1)
    producer_id: str


class RepairPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    primary_seconds: int = Field(default=1800, gt=0)
    primary_attempts: int = Field(default=3, gt=0)
    debug_seconds: int = Field(default=3600, gt=0)
    debug_attempts: int = Field(default=3, gt=0)
    debug_intelligence_class: str
    debug_profile_id: str | None = None
