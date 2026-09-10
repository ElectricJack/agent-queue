"""Typed API contract for durable dashboard state documents."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


Namespace = Literal[
    "nav_organization",
    "shell_preferences",
    "command_center_preferences",
    "command_center_project_view",
    "playbook_graph_view",
]

_NAMESPACE_VALUES = [
    "nav_organization",
    "shell_preferences",
    "command_center_preferences",
    "command_center_project_view",
    "playbook_graph_view",
]
NamespaceInput = Annotated[str, Field(json_schema_extra={"enum": _NAMESPACE_VALUES})]

NonEmptyId = Annotated[str, StringConstraints(min_length=1, max_length=64)]
FolderName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
PaneWidth = Annotated[int, Field(ge=200, le=800)]


class DashboardValueModel(BaseModel):
    """Strict base shared by every persisted namespace value."""

    model_config = ConfigDict(extra="forbid", validate_default=True)


class NavFolder(DashboardValueModel):
    id: NonEmptyId
    name: FolderName
    collapsed: bool = False


class NavOrganization(DashboardValueModel):
    folders: list[NavFolder] = Field(default_factory=list, max_length=200)
    assignments: dict[str, str] = Field(default_factory=dict, max_length=2000)
    project_order: list[str] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="after")
    def validate_references(self) -> "NavOrganization":
        folder_ids = [folder.id for folder in self.folders]
        if len(folder_ids) != len(set(folder_ids)):
            raise ValueError("folder ids must be unique")
        known = set(folder_ids)
        missing = next(
            (
                (project_id, folder_id)
                for project_id, folder_id in self.assignments.items()
                if folder_id not in known
            ),
            None,
        )
        if missing is not None:
            raise ValueError(
                f"assignments.{missing[0]} references unknown folder id {missing[1]!r}"
            )
        if len(self.project_order) != len(set(self.project_order)):
            raise ValueError("project_order must not contain duplicates")
        return self


class RightSurfacePane(DashboardValueModel):
    view: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)


class RightSurface(DashboardValueModel):
    width: int = Field(default=480, ge=280, le=800)
    kind: Literal["pane", "drawer"] | None = None
    activity_tab: Literal["gates", "events"] = "gates"
    pane: RightSurfacePane | None = None


class ShellPreferences(DashboardValueModel):
    theme: Literal["dark", "light", "system"] = "dark"
    pane_widths: dict[str, PaneWidth] = Field(default_factory=dict, max_length=64)
    right_surface: RightSurface = Field(default_factory=RightSurface)
    projects_section_open: bool = True
    agent_flock_collapsed: bool = False
    last_project_id: str | None = None


class CommandCenterPreferences(DashboardValueModel):
    density: Literal["compact", "comfortable", "spacious"] = "comfortable"


class ManualPosition(DashboardValueModel):
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)

    @field_validator("x", "y")
    @classmethod
    def one_decimal_place(cls, value: float) -> float:
        if abs(value * 10 - round(value * 10)) > 1e-7:
            raise ValueError("must use at most one decimal place")
        return value


class CommandCenterProjectView(DashboardValueModel):
    expanded_task_ids: list[str] = Field(default_factory=list, max_length=5000)
    expanded_finished_task_ids: list[str] = Field(default_factory=list, max_length=5000)
    manual_positions: dict[str, ManualPosition] = Field(default_factory=dict, max_length=5000)

    @model_validator(mode="after")
    def validate_expansion(self) -> "CommandCenterProjectView":
        if len(self.expanded_task_ids) != len(set(self.expanded_task_ids)):
            raise ValueError("expanded_task_ids must not contain duplicates")
        if len(self.expanded_finished_task_ids) != len(set(self.expanded_finished_task_ids)):
            raise ValueError("expanded_finished_task_ids must not contain duplicates")
        unknown = set(self.expanded_finished_task_ids) - set(self.expanded_task_ids)
        if unknown:
            raise ValueError("expanded_finished_task_ids must be a subset of expanded_task_ids")
        return self


class PlaybookGraphView(DashboardValueModel):
    manual_positions: dict[str, ManualPosition] = Field(default_factory=dict, max_length=2000)


class DocumentBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["workspace", "user"]
    owner_id: str
    subject: str | None
    revision: int = Field(ge=0)
    exists: bool
    updated_at: float | None


class NavOrganizationDocument(DocumentBase):
    namespace: Literal["nav_organization"]
    value: NavOrganization


class ShellPreferencesDocument(DocumentBase):
    namespace: Literal["shell_preferences"]
    value: ShellPreferences


class CommandCenterPreferencesDocument(DocumentBase):
    namespace: Literal["command_center_preferences"]
    value: CommandCenterPreferences


class CommandCenterProjectViewDocument(DocumentBase):
    namespace: Literal["command_center_project_view"]
    value: CommandCenterProjectView


class PlaybookGraphViewDocument(DocumentBase):
    namespace: Literal["playbook_graph_view"]
    value: PlaybookGraphView


Document = Annotated[
    NavOrganizationDocument
    | ShellPreferencesDocument
    | CommandCenterPreferencesDocument
    | CommandCenterProjectViewDocument
    | PlaybookGraphViewDocument,
    Field(discriminator="namespace"),
]


class DashboardStateListResponse(BaseModel):
    success: bool
    owner_id: str
    documents: list[Document]


class DashboardStateListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DashboardStateDocumentResponse(BaseModel):
    success: bool
    document: Document


class DashboardStateGetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The runtime accepts any string so the command can return its stable
    # ``unknown_namespace`` envelope. The OpenAPI schema remains an enum.
    namespace: NamespaceInput
    subject: str | None = None


class DashboardStateResetRequest(DashboardStateGetRequest):
    pass


class PutBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str | None = None
    base_revision: int | None = Field(default=None, ge=0)


class NavOrganizationPut(PutBase):
    namespace: Literal["nav_organization"]
    value: NavOrganization


class ShellPreferencesPut(PutBase):
    namespace: Literal["shell_preferences"]
    value: ShellPreferences


class CommandCenterPreferencesPut(PutBase):
    namespace: Literal["command_center_preferences"]
    value: CommandCenterPreferences


class CommandCenterProjectViewPut(PutBase):
    namespace: Literal["command_center_project_view"]
    value: CommandCenterProjectView


class PlaybookGraphViewPut(PutBase):
    namespace: Literal["playbook_graph_view"]
    value: PlaybookGraphView


DashboardStateTypedPutRequest = Annotated[
    NavOrganizationPut
    | ShellPreferencesPut
    | CommandCenterPreferencesPut
    | CommandCenterProjectViewPut
    | PlaybookGraphViewPut,
    Field(discriminator="namespace"),
]


class DashboardStatePutRequest(BaseModel):
    """Flat fallback keeps command-level validation/error envelopes intact.

    Response documents still carry the discriminated value union, so both
    generated clients expose every namespace value type without FastAPI
    pre-empting ``invalid_value`` and ``unknown_namespace`` with its generic
    request-validation response.
    """

    model_config = ConfigDict(extra="forbid")

    namespace: NamespaceInput
    subject: str | None = None
    base_revision: int | None = Field(default=None, ge=0)
    value: dict[str, Any]


class DashboardStateErrorResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    success: bool = False
    error_code: str
    error: str


class DashboardStateConflictResponse(BaseModel):
    success: bool = False
    error_code: Literal["revision_conflict"]
    error: str
    current: Document


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "dashboard_state_list": DashboardStateListResponse,
    "dashboard_state_get": DashboardStateDocumentResponse,
    "dashboard_state_put": DashboardStateDocumentResponse,
    "dashboard_state_reset": DashboardStateDocumentResponse,
}

REQUEST_MODELS: dict[str, Any] = {
    "dashboard_state_list": DashboardStateListRequest,
    "dashboard_state_get": DashboardStateGetRequest,
    "dashboard_state_put": DashboardStatePutRequest,
    "dashboard_state_reset": DashboardStateResetRequest,
}


__all__ = [
    "CommandCenterPreferences",
    "CommandCenterProjectView",
    "DashboardStateConflictResponse",
    "DashboardStateDocumentResponse",
    "DashboardStateErrorResponse",
    "DashboardStateGetRequest",
    "DashboardStateListResponse",
    "DashboardStatePutRequest",
    "DashboardStateResetRequest",
    "Document",
    "ManualPosition",
    "Namespace",
    "NavOrganization",
    "PlaybookGraphView",
    "ShellPreferences",
]
