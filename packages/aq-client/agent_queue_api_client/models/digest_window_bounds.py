from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DigestWindowBounds")


@_attrs_define
class DigestWindowBounds:
    """
    Attributes:
        since (float):
        until (float):
        catchup (bool | Unset):  Default: False.
    """

    since: float
    until: float
    catchup: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        since = self.since

        until = self.until

        catchup = self.catchup

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "since": since,
                "until": until,
            }
        )
        if catchup is not UNSET:
            field_dict["catchup"] = catchup

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        since = d.pop("since")

        until = d.pop("until")

        catchup = d.pop("catchup", UNSET)

        digest_window_bounds = cls(
            since=since,
            until=until,
            catchup=catchup,
        )

        digest_window_bounds.additional_properties = d
        return digest_window_bounds

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
