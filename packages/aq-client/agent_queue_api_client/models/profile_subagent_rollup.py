from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProfileSubagentRollup")


@_attrs_define
class ProfileSubagentRollup:
    """
    Attributes:
        active_total (int | Unset):  Default: 0.
        native_total (int | Unset):  Default: 0.
        aq_total (int | Unset):  Default: 0.
        spawned_total (int | Unset):  Default: 0.
        complete (bool | Unset):  Default: True.
        profile_id (str | Unset):  Default: ''.
    """

    active_total: int | Unset = 0
    native_total: int | Unset = 0
    aq_total: int | Unset = 0
    spawned_total: int | Unset = 0
    complete: bool | Unset = True
    profile_id: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        active_total = self.active_total

        native_total = self.native_total

        aq_total = self.aq_total

        spawned_total = self.spawned_total

        complete = self.complete

        profile_id = self.profile_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if active_total is not UNSET:
            field_dict["active_total"] = active_total
        if native_total is not UNSET:
            field_dict["native_total"] = native_total
        if aq_total is not UNSET:
            field_dict["aq_total"] = aq_total
        if spawned_total is not UNSET:
            field_dict["spawned_total"] = spawned_total
        if complete is not UNSET:
            field_dict["complete"] = complete
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        active_total = d.pop("active_total", UNSET)

        native_total = d.pop("native_total", UNSET)

        aq_total = d.pop("aq_total", UNSET)

        spawned_total = d.pop("spawned_total", UNSET)

        complete = d.pop("complete", UNSET)

        profile_id = d.pop("profile_id", UNSET)

        profile_subagent_rollup = cls(
            active_total=active_total,
            native_total=native_total,
            aq_total=aq_total,
            spawned_total=spawned_total,
            complete=complete,
            profile_id=profile_id,
        )

        profile_subagent_rollup.additional_properties = d
        return profile_subagent_rollup

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
