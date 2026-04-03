from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreateProjectResponse")


@_attrs_define
class CreateProjectResponse:
    """
    Attributes:
        created (str):
        name (str):
        auto_create_channels (bool | Unset):  Default: False.
    """

    created: str
    name: str
    auto_create_channels: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        created = self.created

        name = self.name

        auto_create_channels = self.auto_create_channels

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "created": created,
                "name": name,
            }
        )
        if auto_create_channels is not UNSET:
            field_dict["auto_create_channels"] = auto_create_channels

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        created = d.pop("created")

        name = d.pop("name")

        auto_create_channels = d.pop("auto_create_channels", UNSET)

        create_project_response = cls(
            created=created,
            name=name,
            auto_create_channels=auto_create_channels,
        )

        create_project_response.additional_properties = d
        return create_project_response

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
