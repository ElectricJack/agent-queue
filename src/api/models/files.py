"""Response models for file commands."""

from __future__ import annotations


from pydantic import BaseModel


class FileEntry(BaseModel):
    name: str
    size: int = 0


class ReadFileResponse(BaseModel):
    content: str = ""
    path: str = ""
    offset: int | None = None
    truncated: bool | None = None
    total_lines: int | None = None
    lines_returned: int | None = None


class WriteFileResponse(BaseModel):
    path: str = ""
    written: int = 0


class EditFileResponse(BaseModel):
    path: str = ""
    replacements: int = 0


class GlobFilesResponse(BaseModel):
    matches: list[str] = []
    count: int = 0
    truncated: bool | None = None
    total: int | None = None


class GrepResponse(BaseModel):
    results: str = ""
    mode: str = ""


class SearchFilesResponse(BaseModel):
    results: str = ""
    mode: str = ""


class ListDirectoryResponse(BaseModel):
    project_id: str = ""
    path: str = ""
    workspace_path: str = ""
    workspace_name: str = ""
    directories: list[str] = []
    files: list[FileEntry] = []


# --- Project memory + inspection commands ---------------------------------


class ReadProjectMemoryFileResponse(BaseModel):
    project_id: str
    path: str
    content: str = ""


class CountProjectMemoryFilesResponse(BaseModel):
    project_id: str
    path: str
    count: int = 0
    total: int = 0
    missing: bool | None = None
    newer_than: str | None = None


class SelectFilesForInspectionResponse(BaseModel):
    project_id: str
    workspace_name: str = ""
    workspace_path: str = ""
    files: list[str] = []
    categorized: dict[str, list[str]] = {}
    weights: dict[str, float] = {}
    target_counts: dict[str, int] = {}
    total_enumerated: int = 0
    excluded_history: int = 0
    history_files: list[str] = []
    history_lookback_days: int = 0


class FileInspectionRecord(BaseModel):
    model_config = {"extra": "allow"}


class RecordFileInspectionResponse(BaseModel):
    recorded: bool = True
    project_id: str
    file_path: str
    key: str
    record: FileInspectionRecord
    warning: str | None = None


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "read_file": ReadFileResponse,
    "write_file": WriteFileResponse,
    "edit_file": EditFileResponse,
    "glob_files": GlobFilesResponse,
    "grep": GrepResponse,
    "search_files": SearchFilesResponse,
    "list_directory": ListDirectoryResponse,
    "read_project_memory_file": ReadProjectMemoryFileResponse,
    "count_project_memory_files": CountProjectMemoryFilesResponse,
    "select_files_for_inspection": SelectFilesForInspectionResponse,
    "record_file_inspection": RecordFileInspectionResponse,
}
