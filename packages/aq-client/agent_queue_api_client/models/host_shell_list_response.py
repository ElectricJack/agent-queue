from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.host_shell_info import HostShellInfo


T = TypeVar("T", bound="HostShellListResponse")


@_attrs_define
class HostShellListResponse:
    """
    Attributes:
        enabled (bool):
        shells (list[HostShellInfo]):
    """

    enabled: bool
    shells: list[HostShellInfo]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        enabled = self.enabled

        shells = []
        for shells_item_data in self.shells:
            shells_item = shells_item_data.to_dict()
            shells.append(shells_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "enabled": enabled,
                "shells": shells,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.host_shell_info import HostShellInfo

        d = dict(src_dict)
        enabled = d.pop("enabled")

        shells = []
        _shells = d.pop("shells")
        for shells_item_data in _shells:
            shells_item = HostShellInfo.from_dict(shells_item_data)

            shells.append(shells_item)

        host_shell_list_response = cls(
            enabled=enabled,
            shells=shells,
        )

        host_shell_list_response.additional_properties = d
        return host_shell_list_response

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
