from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="AgentSummary")


@_attrs_define
class AgentSummary:
    """
    Attributes:
        workspace_id (str):
        project_id (str):
        name (str | Unset):  Default: ''.
        state (str | Unset):  Default: ''.
        current_task_id (None | str | Unset):
        current_task_title (None | str | Unset):
    """

    workspace_id: str
    project_id: str
    name: str | Unset = ""
    state: str | Unset = ""
    current_task_id: None | str | Unset = UNSET
    current_task_title: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        workspace_id = self.workspace_id

        project_id = self.project_id

        name = self.name

        state = self.state

        current_task_id: None | str | Unset
        if isinstance(self.current_task_id, Unset):
            current_task_id = UNSET
        else:
            current_task_id = self.current_task_id

        current_task_title: None | str | Unset
        if isinstance(self.current_task_title, Unset):
            current_task_title = UNSET
        else:
            current_task_title = self.current_task_title

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "workspace_id": workspace_id,
                "project_id": project_id,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if state is not UNSET:
            field_dict["state"] = state
        if current_task_id is not UNSET:
            field_dict["current_task_id"] = current_task_id
        if current_task_title is not UNSET:
            field_dict["current_task_title"] = current_task_title

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        workspace_id = d.pop("workspace_id")

        project_id = d.pop("project_id")

        name = d.pop("name", UNSET)

        state = d.pop("state", UNSET)

        def _parse_current_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_task_id = _parse_current_task_id(d.pop("current_task_id", UNSET))

        def _parse_current_task_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_task_title = _parse_current_task_title(d.pop("current_task_title", UNSET))

        agent_summary = cls(
            workspace_id=workspace_id,
            project_id=project_id,
            name=name,
            state=state,
            current_task_id=current_task_id,
            current_task_title=current_task_title,
        )

        agent_summary.additional_properties = d
        return agent_summary

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
