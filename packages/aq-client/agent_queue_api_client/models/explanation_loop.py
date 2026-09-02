from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ExplanationLoop")


@_attrs_define
class ExplanationLoop:
    """
    Attributes:
        source_text (str):
        item_binding (str):
        source_raw (None | str | Unset):
    """

    source_text: str
    item_binding: str
    source_raw: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        source_text = self.source_text

        item_binding = self.item_binding

        source_raw: None | str | Unset
        if isinstance(self.source_raw, Unset):
            source_raw = UNSET
        else:
            source_raw = self.source_raw

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "source_text": source_text,
                "item_binding": item_binding,
            }
        )
        if source_raw is not UNSET:
            field_dict["source_raw"] = source_raw

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        source_text = d.pop("source_text")

        item_binding = d.pop("item_binding")

        def _parse_source_raw(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source_raw = _parse_source_raw(d.pop("source_raw", UNSET))

        explanation_loop = cls(
            source_text=source_text,
            item_binding=item_binding,
            source_raw=source_raw,
        )

        explanation_loop.additional_properties = d
        return explanation_loop

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
