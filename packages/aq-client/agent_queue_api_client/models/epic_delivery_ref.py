from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.epic_delivery_ref_kind import EpicDeliveryRefKind
from ..types import UNSET, Unset

T = TypeVar("T", bound="EpicDeliveryRef")


@_attrs_define
class EpicDeliveryRef:
    """A party or record an epic's delivery status points at.

    ``task``/``operation``/``batch``/``gate`` name a record (``id`` set);
    ``session`` is the live worker session; ``system``/``operator``/
    ``supervisor`` name who must act and carry no ``id``.

        Attributes:
            kind (EpicDeliveryRefKind):
            id (None | str | Unset):
            label (str | Unset):  Default: ''.
    """

    kind: EpicDeliveryRefKind
    id: None | str | Unset = UNSET
    label: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind.value

        id: None | str | Unset
        if isinstance(self.id, Unset):
            id = UNSET
        else:
            id = self.id

        label = self.label

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
            }
        )
        if id is not UNSET:
            field_dict["id"] = id
        if label is not UNSET:
            field_dict["label"] = label

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = EpicDeliveryRefKind(d.pop("kind"))

        def _parse_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        id = _parse_id(d.pop("id", UNSET))

        label = d.pop("label", UNSET)

        epic_delivery_ref = cls(
            kind=kind,
            id=id,
            label=label,
        )

        epic_delivery_ref.additional_properties = d
        return epic_delivery_ref

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
