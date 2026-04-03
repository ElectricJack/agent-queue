from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WorkspaceSummary")


@_attrs_define
class WorkspaceSummary:
    """
    Attributes:
        id (str):
        project_id (str):
        workspace_path (str):
        source_type (str | Unset):  Default: ''.
        name (None | str | Unset):
        locked_by_agent_id (None | str | Unset):
        locked_by_task_id (None | str | Unset):
    """

    id: str
    project_id: str
    workspace_path: str
    source_type: str | Unset = ""
    name: None | str | Unset = UNSET
    locked_by_agent_id: None | str | Unset = UNSET
    locked_by_task_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        project_id = self.project_id

        workspace_path = self.workspace_path

        source_type = self.source_type

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        locked_by_agent_id: None | str | Unset
        if isinstance(self.locked_by_agent_id, Unset):
            locked_by_agent_id = UNSET
        else:
            locked_by_agent_id = self.locked_by_agent_id

        locked_by_task_id: None | str | Unset
        if isinstance(self.locked_by_task_id, Unset):
            locked_by_task_id = UNSET
        else:
            locked_by_task_id = self.locked_by_task_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "project_id": project_id,
                "workspace_path": workspace_path,
            }
        )
        if source_type is not UNSET:
            field_dict["source_type"] = source_type
        if name is not UNSET:
            field_dict["name"] = name
        if locked_by_agent_id is not UNSET:
            field_dict["locked_by_agent_id"] = locked_by_agent_id
        if locked_by_task_id is not UNSET:
            field_dict["locked_by_task_id"] = locked_by_task_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        project_id = d.pop("project_id")

        workspace_path = d.pop("workspace_path")

        source_type = d.pop("source_type", UNSET)

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        def _parse_locked_by_agent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        locked_by_agent_id = _parse_locked_by_agent_id(d.pop("locked_by_agent_id", UNSET))

        def _parse_locked_by_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        locked_by_task_id = _parse_locked_by_task_id(d.pop("locked_by_task_id", UNSET))

        workspace_summary = cls(
            id=id,
            project_id=project_id,
            workspace_path=workspace_path,
            source_type=source_type,
            name=name,
            locked_by_agent_id=locked_by_agent_id,
            locked_by_task_id=locked_by_task_id,
        )

        workspace_summary.additional_properties = d
        return workspace_summary

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
