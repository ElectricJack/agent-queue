"""Response models for memory commands."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class MemorySearchResult(BaseModel):
    rank: int = 0
    source: str = ""
    heading: str = ""
    content: str = ""
    score: float = 0.0


class NoteSummary(BaseModel):
    name: str = ""
    title: str = ""
    size_bytes: int = 0
    modified: str | None = None
    path: str | None = None


class MemorySearchResponse(BaseModel):
    project_id: str
    query: str = ""
    top_k: int = 0
    count: int = 0
    results: list[MemorySearchResult] = []


class MemoryStatsResponse(BaseModel):
    model_config = {"extra": "allow"}
    project_id: str


class MemoryReindexResponse(BaseModel):
    project_id: str
    status: str = "reindex_complete"
    chunks_indexed: int = 0


class CompactMemoryResponse(BaseModel):
    model_config = {"extra": "allow"}
    project_id: str


class ListNotesResponse(BaseModel):
    project_id: str
    notes: list[NoteSummary] = []


class ReadNoteResponse(BaseModel):
    content: str = ""
    title: str = ""
    path: str = ""
    size_bytes: int = 0


class WriteNoteResponse(BaseModel):
    path: str = ""
    title: str = ""
    status: str = ""


class AppendNoteResponse(BaseModel):
    path: str = ""
    title: str = ""
    status: str = ""
    size_bytes: int = 0


class DeleteNoteResponse(BaseModel):
    deleted: str
    title: str = ""


class PromoteNoteResponse(BaseModel):
    project_id: str
    note: str | None = None
    status: str = ""
    message: str = ""
    profile_preview: str | None = None


class ViewProfileResponse(BaseModel):
    project_id: str
    profile: str | None = None
    message: str | None = None


class EditProjectProfileResponse(BaseModel):
    project_id: str
    status: str = "profile_updated"
    path: str = ""


class RegenerateProfileResponse(BaseModel):
    project_id: str
    status: str = ""
    profile: str | None = None
    message: str | None = None


class CompareSpecsNotesResponse(BaseModel):
    specs: list[NoteSummary] = []
    notes: list[NoteSummary] = []
    specs_path: str = ""
    notes_path: str = ""
    project_id: str = ""


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "memory_search": MemorySearchResponse,
    "memory_stats": MemoryStatsResponse,
    "memory_reindex": MemoryReindexResponse,
    "compact_memory": CompactMemoryResponse,
    "list_notes": ListNotesResponse,
    "read_note": ReadNoteResponse,
    "write_note": WriteNoteResponse,
    "append_note": AppendNoteResponse,
    "delete_note": DeleteNoteResponse,
    "promote_note": PromoteNoteResponse,
    "view_profile": ViewProfileResponse,
    "edit_project_profile": EditProjectProfileResponse,
    "regenerate_profile": RegenerateProfileResponse,
    "compare_specs_notes": CompareSpecsNotesResponse,
}
