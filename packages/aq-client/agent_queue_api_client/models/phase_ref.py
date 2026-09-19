from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PhaseRef")


@_attrs_define
class PhaseRef:
    """The phase ``phase_create`` just wrote.

    Attributes:
        id (str):
        order (int):
        label (str):
        parent_id (None | str | Unset):
        blocked_by (None | str | Unset):
    """

    id: str
    order: int
    label: str
    parent_id: None | str | Unset = UNSET
    blocked_by: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        order = self.order

        label = self.label

        parent_id: None | str | Unset
        if isinstance(self.parent_id, Unset):
            parent_id = UNSET
        else:
            parent_id = self.parent_id

        blocked_by: None | str | Unset
        if isinstance(self.blocked_by, Unset):
            blocked_by = UNSET
        else:
            blocked_by = self.blocked_by

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "order": order,
                "label": label,
            }
        )
        if parent_id is not UNSET:
            field_dict["parent_id"] = parent_id
        if blocked_by is not UNSET:
            field_dict["blocked_by"] = blocked_by

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        order = d.pop("order")

        label = d.pop("label")

        def _parse_parent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_id = _parse_parent_id(d.pop("parent_id", UNSET))

        def _parse_blocked_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        blocked_by = _parse_blocked_by(d.pop("blocked_by", UNSET))

        phase_ref = cls(
            id=id,
            order=order,
            label=label,
            parent_id=parent_id,
            blocked_by=blocked_by,
        )

        phase_ref.additional_properties = d
        return phase_ref

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
