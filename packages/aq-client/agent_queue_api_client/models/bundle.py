from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.item import Item
    from ..models.placeholder import Placeholder


T = TypeVar("T", bound="Bundle")


@_attrs_define
class Bundle:
    """
    Attributes:
        name (str):
        source_aq_version (str):
        items (list[Item]):
        format_ (Literal['aq.policy.v1'] | Unset):  Default: 'aq.policy.v1'.
        version (Literal[1] | Unset):  Default: 1.
        placeholders (list[Placeholder] | Unset):
    """

    name: str
    source_aq_version: str
    items: list[Item]
    format_: Literal["aq.policy.v1"] | Unset = "aq.policy.v1"
    version: Literal[1] | Unset = 1
    placeholders: list[Placeholder] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        source_aq_version = self.source_aq_version

        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        format_ = self.format_

        version = self.version

        placeholders: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.placeholders, Unset):
            placeholders = []
            for placeholders_item_data in self.placeholders:
                placeholders_item = placeholders_item_data.to_dict()
                placeholders.append(placeholders_item)

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "name": name,
                "source_aq_version": source_aq_version,
                "items": items,
            }
        )
        if format_ is not UNSET:
            field_dict["format"] = format_
        if version is not UNSET:
            field_dict["version"] = version
        if placeholders is not UNSET:
            field_dict["placeholders"] = placeholders

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.item import Item
        from ..models.placeholder import Placeholder

        d = dict(src_dict)
        name = d.pop("name")

        source_aq_version = d.pop("source_aq_version")

        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = Item.from_dict(items_item_data)

            items.append(items_item)

        format_ = cast(Literal["aq.policy.v1"] | Unset, d.pop("format", UNSET))
        if format_ != "aq.policy.v1" and not isinstance(format_, Unset):
            raise ValueError(f"format must match const 'aq.policy.v1', got '{format_}'")

        version = cast(Literal[1] | Unset, d.pop("version", UNSET))
        if version != 1 and not isinstance(version, Unset):
            raise ValueError(f"version must match const 1, got '{version}'")

        _placeholders = d.pop("placeholders", UNSET)
        placeholders: list[Placeholder] | Unset = UNSET
        if _placeholders is not UNSET:
            placeholders = []
            for placeholders_item_data in _placeholders:
                placeholders_item = Placeholder.from_dict(placeholders_item_data)

                placeholders.append(placeholders_item)

        bundle = cls(
            name=name,
            source_aq_version=source_aq_version,
            items=items,
            format_=format_,
            version=version,
            placeholders=placeholders,
        )

        return bundle
