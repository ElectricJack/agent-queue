from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_allocation_diagnostic import ProviderAllocationDiagnostic
    from ..models.provider_allocation_group import ProviderAllocationGroup
    from ..models.provider_allocation_project import ProviderAllocationProject


T = TypeVar("T", bound="ProviderAllocationStatusResponse")


@_attrs_define
class ProviderAllocationStatusResponse:
    """``provider_allocation_status`` and ``GET /api/providers/allocation``.

    Attributes:
        now (float):
        success (bool | Unset):  Default: True.
        project_id (None | str | Unset):
        redacted (bool | Unset):  Default: False.
        global_max_active (int | None | Unset):
        providers (list[ProviderAllocationGroup] | Unset):
        projects (list[ProviderAllocationProject] | Unset):
        diagnostics (list[ProviderAllocationDiagnostic] | Unset):
    """

    now: float
    success: bool | Unset = True
    project_id: None | str | Unset = UNSET
    redacted: bool | Unset = False
    global_max_active: int | None | Unset = UNSET
    providers: list[ProviderAllocationGroup] | Unset = UNSET
    projects: list[ProviderAllocationProject] | Unset = UNSET
    diagnostics: list[ProviderAllocationDiagnostic] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        now = self.now

        success = self.success

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        redacted = self.redacted

        global_max_active: int | None | Unset
        if isinstance(self.global_max_active, Unset):
            global_max_active = UNSET
        else:
            global_max_active = self.global_max_active

        providers: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.providers, Unset):
            providers = []
            for providers_item_data in self.providers:
                providers_item = providers_item_data.to_dict()
                providers.append(providers_item)

        projects: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.projects, Unset):
            projects = []
            for projects_item_data in self.projects:
                projects_item = projects_item_data.to_dict()
                projects.append(projects_item)

        diagnostics: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.diagnostics, Unset):
            diagnostics = []
            for diagnostics_item_data in self.diagnostics:
                diagnostics_item = diagnostics_item_data.to_dict()
                diagnostics.append(diagnostics_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "now": now,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if redacted is not UNSET:
            field_dict["redacted"] = redacted
        if global_max_active is not UNSET:
            field_dict["global_max_active"] = global_max_active
        if providers is not UNSET:
            field_dict["providers"] = providers
        if projects is not UNSET:
            field_dict["projects"] = projects
        if diagnostics is not UNSET:
            field_dict["diagnostics"] = diagnostics

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_allocation_diagnostic import ProviderAllocationDiagnostic
        from ..models.provider_allocation_group import ProviderAllocationGroup
        from ..models.provider_allocation_project import ProviderAllocationProject

        d = dict(src_dict)
        now = d.pop("now")

        success = d.pop("success", UNSET)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        redacted = d.pop("redacted", UNSET)

        def _parse_global_max_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        global_max_active = _parse_global_max_active(d.pop("global_max_active", UNSET))

        _providers = d.pop("providers", UNSET)
        providers: list[ProviderAllocationGroup] | Unset = UNSET
        if _providers is not UNSET:
            providers = []
            for providers_item_data in _providers:
                providers_item = ProviderAllocationGroup.from_dict(providers_item_data)

                providers.append(providers_item)

        _projects = d.pop("projects", UNSET)
        projects: list[ProviderAllocationProject] | Unset = UNSET
        if _projects is not UNSET:
            projects = []
            for projects_item_data in _projects:
                projects_item = ProviderAllocationProject.from_dict(projects_item_data)

                projects.append(projects_item)

        _diagnostics = d.pop("diagnostics", UNSET)
        diagnostics: list[ProviderAllocationDiagnostic] | Unset = UNSET
        if _diagnostics is not UNSET:
            diagnostics = []
            for diagnostics_item_data in _diagnostics:
                diagnostics_item = ProviderAllocationDiagnostic.from_dict(diagnostics_item_data)

                diagnostics.append(diagnostics_item)

        provider_allocation_status_response = cls(
            now=now,
            success=success,
            project_id=project_id,
            redacted=redacted,
            global_max_active=global_max_active,
            providers=providers,
            projects=projects,
            diagnostics=diagnostics,
        )

        provider_allocation_status_response.additional_properties = d
        return provider_allocation_status_response

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
