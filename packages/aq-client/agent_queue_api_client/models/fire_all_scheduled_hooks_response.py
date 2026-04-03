from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="FireAllScheduledHooksResponse")


@_attrs_define
class FireAllScheduledHooksResponse:
    """
    Attributes:
        fired (list[str] | Unset):
        count (int | Unset):  Default: 0.
    """

    fired: list[str] | Unset = UNSET
    count: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        fired: list[str] | Unset = UNSET
        if not isinstance(self.fired, Unset):
            fired = self.fired

        count = self.count

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if fired is not UNSET:
            field_dict["fired"] = fired
        if count is not UNSET:
            field_dict["count"] = count

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        fired = cast(list[str], d.pop("fired", UNSET))

        count = d.pop("count", UNSET)

        fire_all_scheduled_hooks_response = cls(
            fired=fired,
            count=count,
        )

        fire_all_scheduled_hooks_response.additional_properties = d
        return fire_all_scheduled_hooks_response

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
