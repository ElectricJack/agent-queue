from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SlotMetrics")


@_attrs_define
class SlotMetrics:
    """Worktree slots.  ``cap`` is null when worktree execution is off.

    Attributes:
        used (float | Unset):  Default: 0.0.
        total (float | Unset):  Default: 0.0.
        cap (float | None | Unset):
    """

    used: float | Unset = 0.0
    total: float | Unset = 0.0
    cap: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        used = self.used

        total = self.total

        cap: float | None | Unset
        if isinstance(self.cap, Unset):
            cap = UNSET
        else:
            cap = self.cap

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if used is not UNSET:
            field_dict["used"] = used
        if total is not UNSET:
            field_dict["total"] = total
        if cap is not UNSET:
            field_dict["cap"] = cap

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        used = d.pop("used", UNSET)

        total = d.pop("total", UNSET)

        def _parse_cap(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        cap = _parse_cap(d.pop("cap", UNSET))

        slot_metrics = cls(
            used=used,
            total=total,
            cap=cap,
        )

        slot_metrics.additional_properties = d
        return slot_metrics

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
