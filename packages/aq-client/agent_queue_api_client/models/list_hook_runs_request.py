from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ListHookRunsRequest")


@_attrs_define
class ListHookRunsRequest:
    """
    Attributes:
        hook_id (str): Hook ID
        limit (int | Unset): Number of runs to show (default 10) Default: 10.
    """

    hook_id: str
    limit: int | Unset = 10
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        hook_id = self.hook_id

        limit = self.limit

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "hook_id": hook_id,
            }
        )
        if limit is not UNSET:
            field_dict["limit"] = limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        hook_id = d.pop("hook_id")

        limit = d.pop("limit", UNSET)

        list_hook_runs_request = cls(
            hook_id=hook_id,
            limit=limit,
        )

        list_hook_runs_request.additional_properties = d
        return list_hook_runs_request

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
