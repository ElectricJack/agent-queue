from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderAllocationBusySet")


@_attrs_define
class ProviderAllocationBusySet:
    """The busy sessions (and their tasks) the request stops.

    Attributes:
        session_ids (list[str] | Unset):
        task_ids (list[str] | Unset):
    """

    session_ids: list[str] | Unset = UNSET
    task_ids: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        session_ids: list[str] | Unset = UNSET
        if not isinstance(self.session_ids, Unset):
            session_ids = self.session_ids

        task_ids: list[str] | Unset = UNSET
        if not isinstance(self.task_ids, Unset):
            task_ids = self.task_ids

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if session_ids is not UNSET:
            field_dict["session_ids"] = session_ids
        if task_ids is not UNSET:
            field_dict["task_ids"] = task_ids

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        session_ids = cast(list[str], d.pop("session_ids", UNSET))

        task_ids = cast(list[str], d.pop("task_ids", UNSET))

        provider_allocation_busy_set = cls(
            session_ids=session_ids,
            task_ids=task_ids,
        )

        provider_allocation_busy_set.additional_properties = d
        return provider_allocation_busy_set

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
