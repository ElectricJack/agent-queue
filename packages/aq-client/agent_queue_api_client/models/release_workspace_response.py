from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReleaseWorkspaceResponse")


@_attrs_define
class ReleaseWorkspaceResponse:
    """
    Attributes:
        workspace_id (str):
        released_from_agent (None | str | Unset):
        released_from_task (None | str | Unset):
    """

    workspace_id: str
    released_from_agent: None | str | Unset = UNSET
    released_from_task: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        workspace_id = self.workspace_id

        released_from_agent: None | str | Unset
        if isinstance(self.released_from_agent, Unset):
            released_from_agent = UNSET
        else:
            released_from_agent = self.released_from_agent

        released_from_task: None | str | Unset
        if isinstance(self.released_from_task, Unset):
            released_from_task = UNSET
        else:
            released_from_task = self.released_from_task

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "workspace_id": workspace_id,
            }
        )
        if released_from_agent is not UNSET:
            field_dict["released_from_agent"] = released_from_agent
        if released_from_task is not UNSET:
            field_dict["released_from_task"] = released_from_task

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        workspace_id = d.pop("workspace_id")

        def _parse_released_from_agent(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        released_from_agent = _parse_released_from_agent(d.pop("released_from_agent", UNSET))

        def _parse_released_from_task(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        released_from_task = _parse_released_from_task(d.pop("released_from_task", UNSET))

        release_workspace_response = cls(
            workspace_id=workspace_id,
            released_from_agent=released_from_agent,
            released_from_task=released_from_task,
        )

        release_workspace_response.additional_properties = d
        return release_workspace_response

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
