from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="IntelligenceClassPolicy")


@_attrs_define
class IntelligenceClassPolicy:
    """
    Attributes:
        source (str):
        type_ (Literal['intelligence_class'] | Unset):  Default: 'intelligence_class'.
    """

    source: str
    type_: Literal["intelligence_class"] | Unset = "intelligence_class"

    def to_dict(self) -> dict[str, Any]:
        source = self.source

        type_ = self.type_

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "source": source,
            }
        )
        if type_ is not UNSET:
            field_dict["type"] = type_

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        source = d.pop("source")

        type_ = cast(Literal["intelligence_class"] | Unset, d.pop("type", UNSET))
        if type_ != "intelligence_class" and not isinstance(type_, Unset):
            raise ValueError(f"type must match const 'intelligence_class', got '{type_}'")

        intelligence_class_policy = cls(
            source=source,
            type_=type_,
        )

        return intelligence_class_policy
