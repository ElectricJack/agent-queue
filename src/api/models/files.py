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


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "read_file": ReadFileResponse,
    "write_file": WriteFileResponse,
    "edit_file": EditFileResponse,
    "glob_files": GlobFilesResponse,
    "grep": GrepResponse,
    "search_files": SearchFilesResponse,
    "list_directory": ListDirectoryResponse,
}
