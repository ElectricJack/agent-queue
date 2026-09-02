from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PoolScaleRequest")


@_attrs_define
class PoolScaleRequest:
    """
    Attributes:
        project_id (str): Project ID.
        profile_id (str): Pool profile (agent-type) ID.
        min_ (int | None | Unset): New min_active bound (optional).
        max_ (int | None | Unset): New max_active bound; null removes the profile limit.
        now (bool | None | Unset): Immediately terminate idle sessions above the new max, oldest first.
    """

    project_id: str
    profile_id: str
    min_: int | None | Unset = UNSET
    max_: int | None | Unset = UNSET
    now: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        profile_id = self.profile_id

        min_: int | None | Unset
        if isinstance(self.min_, Unset):
            min_ = UNSET
        else:
            min_ = self.min_

        max_: int | None | Unset
        if isinstance(self.max_, Unset):
            max_ = UNSET
        else:
            max_ = self.max_

        now: bool | None | Unset
        if isinstance(self.now, Unset):
            now = UNSET
        else:
            now = self.now

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "profile_id": profile_id,
            }
        )
        if min_ is not UNSET:
            field_dict["min"] = min_
        if max_ is not UNSET:
            field_dict["max"] = max_
        if now is not UNSET:
            field_dict["now"] = now

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        profile_id = d.pop("profile_id")

        def _parse_min_(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        min_ = _parse_min_(d.pop("min", UNSET))

        def _parse_max_(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_ = _parse_max_(d.pop("max", UNSET))

        def _parse_now(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        now = _parse_now(d.pop("now", UNSET))

        pool_scale_request = cls(
            project_id=project_id,
            profile_id=profile_id,
            min_=min_,
            max_=max_,
            now=now,
        )

        pool_scale_request.additional_properties = d
        return pool_scale_request

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
