from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.workspace_list_diagnostics import WorkspaceListDiagnostics
    from ..models.workspace_summary import WorkspaceSummary


T = TypeVar("T", bound="ListWorkspacesResponse")


@_attrs_define
class ListWorkspacesResponse:
    """
    Attributes:
        workspaces (list[WorkspaceSummary] | Unset):
        diagnostics (None | Unset | WorkspaceListDiagnostics):
    """

    workspaces: list[WorkspaceSummary] | Unset = UNSET
    diagnostics: None | Unset | WorkspaceListDiagnostics = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.workspace_list_diagnostics import WorkspaceListDiagnostics

        workspaces: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.workspaces, Unset):
            workspaces = []
            for workspaces_item_data in self.workspaces:
                workspaces_item = workspaces_item_data.to_dict()
                workspaces.append(workspaces_item)

        diagnostics: dict[str, Any] | None | Unset
        if isinstance(self.diagnostics, Unset):
            diagnostics = UNSET
        elif isinstance(self.diagnostics, WorkspaceListDiagnostics):
            diagnostics = self.diagnostics.to_dict()
        else:
            diagnostics = self.diagnostics

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if workspaces is not UNSET:
            field_dict["workspaces"] = workspaces
        if diagnostics is not UNSET:
            field_dict["diagnostics"] = diagnostics

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.workspace_list_diagnostics import WorkspaceListDiagnostics
        from ..models.workspace_summary import WorkspaceSummary

        d = dict(src_dict)
        _workspaces = d.pop("workspaces", UNSET)
        workspaces: list[WorkspaceSummary] | Unset = UNSET
        if _workspaces is not UNSET:
            workspaces = []
            for workspaces_item_data in _workspaces:
                workspaces_item = WorkspaceSummary.from_dict(workspaces_item_data)

                workspaces.append(workspaces_item)

        def _parse_diagnostics(data: object) -> None | Unset | WorkspaceListDiagnostics:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                diagnostics_type_0 = WorkspaceListDiagnostics.from_dict(data)

                return diagnostics_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WorkspaceListDiagnostics, data)

        diagnostics = _parse_diagnostics(d.pop("diagnostics", UNSET))

        list_workspaces_response = cls(
            workspaces=workspaces,
            diagnostics=diagnostics,
        )

        list_workspaces_response.additional_properties = d
        return list_workspaces_response

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
