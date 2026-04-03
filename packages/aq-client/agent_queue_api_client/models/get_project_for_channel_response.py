from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GetProjectForChannelResponse")


@_attrs_define
class GetProjectForChannelResponse:
    """
    Attributes:
        channel_id (str):
        project_id (None | str | Unset):
        project_name (None | str | Unset):
    """

    channel_id: str
    project_id: None | str | Unset = UNSET
    project_name: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        channel_id = self.channel_id

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        project_name: None | str | Unset
        if isinstance(self.project_name, Unset):
            project_name = UNSET
        else:
            project_name = self.project_name

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "channel_id": channel_id,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if project_name is not UNSET:
            field_dict["project_name"] = project_name

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        channel_id = d.pop("channel_id")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_project_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_name = _parse_project_name(d.pop("project_name", UNSET))

        get_project_for_channel_response = cls(
            channel_id=channel_id,
            project_id=project_id,
            project_name=project_name,
        )

        get_project_for_channel_response.additional_properties = d
        return get_project_for_channel_response

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
