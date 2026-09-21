from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PhaseSummary")


@_attrs_define
class PhaseSummary:
    """One phase as ``phase_list`` reports it.

    Attributes:
        id (str):
        title (str):
        label (str):
        order (int):
        status (str):
        is_blocked (bool | Unset):  Default: False.
        total (int | Unset):  Default: 0.
        done (int | Unset):  Default: 0.
    """

    id: str
    title: str
    label: str
    order: int
    status: str
    is_blocked: bool | Unset = False
    total: int | Unset = 0
    done: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        title = self.title

        label = self.label

        order = self.order

        status = self.status

        is_blocked = self.is_blocked

        total = self.total

        done = self.done

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "title": title,
                "label": label,
                "order": order,
                "status": status,
            }
        )
        if is_blocked is not UNSET:
            field_dict["is_blocked"] = is_blocked
        if total is not UNSET:
            field_dict["total"] = total
        if done is not UNSET:
            field_dict["done"] = done

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        title = d.pop("title")

        label = d.pop("label")

        order = d.pop("order")

        status = d.pop("status")

        is_blocked = d.pop("is_blocked", UNSET)

        total = d.pop("total", UNSET)

        done = d.pop("done", UNSET)

        phase_summary = cls(
            id=id,
            title=title,
            label=label,
            order=order,
            status=status,
            is_blocked=is_blocked,
            total=total,
            done=done,
        )

        phase_summary.additional_properties = d
        return phase_summary

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
