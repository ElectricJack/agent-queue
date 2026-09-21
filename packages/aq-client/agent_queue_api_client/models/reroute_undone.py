from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="RerouteUndone")


@_attrs_define
class RerouteUndone:
    """
    Attributes:
        task_id (str):
        from_profile_id (None | str | Unset):
        to_profile_id (None | str | Unset):
        reroute_id (int | None | Unset):
    """

    task_id: str
    from_profile_id: None | str | Unset = UNSET
    to_profile_id: None | str | Unset = UNSET
    reroute_id: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        from_profile_id: None | str | Unset
        if isinstance(self.from_profile_id, Unset):
            from_profile_id = UNSET
        else:
            from_profile_id = self.from_profile_id

        to_profile_id: None | str | Unset
        if isinstance(self.to_profile_id, Unset):
            to_profile_id = UNSET
        else:
            to_profile_id = self.to_profile_id

        reroute_id: int | None | Unset
        if isinstance(self.reroute_id, Unset):
            reroute_id = UNSET
        else:
            reroute_id = self.reroute_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
            }
        )
        if from_profile_id is not UNSET:
            field_dict["from_profile_id"] = from_profile_id
        if to_profile_id is not UNSET:
            field_dict["to_profile_id"] = to_profile_id
        if reroute_id is not UNSET:
            field_dict["reroute_id"] = reroute_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        def _parse_from_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        from_profile_id = _parse_from_profile_id(d.pop("from_profile_id", UNSET))

        def _parse_to_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        to_profile_id = _parse_to_profile_id(d.pop("to_profile_id", UNSET))

        def _parse_reroute_id(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        reroute_id = _parse_reroute_id(d.pop("reroute_id", UNSET))

        reroute_undone = cls(
            task_id=task_id,
            from_profile_id=from_profile_id,
            to_profile_id=to_profile_id,
            reroute_id=reroute_id,
        )

        reroute_undone.additional_properties = d
        return reroute_undone

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
