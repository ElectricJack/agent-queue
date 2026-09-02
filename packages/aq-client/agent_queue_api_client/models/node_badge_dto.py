from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..models.node_badge_dto_kind import NodeBadgeDTOKind

T = TypeVar("T", bound="NodeBadgeDTO")


@_attrs_define
class NodeBadgeDTO:
    """One compact chip on the card.  Ordered by the backend.

    Attributes:
        kind (NodeBadgeDTOKind):
        label (str):
        value (str):
    """

    kind: NodeBadgeDTOKind
    label: str
    value: str

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind.value

        label = self.label

        value = self.value

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "kind": kind,
                "label": label,
                "value": value,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = NodeBadgeDTOKind(d.pop("kind"))

        label = d.pop("label")

        value = d.pop("value")

        node_badge_dto = cls(
            kind=kind,
            label=label,
            value=value,
        )

        return node_badge_dto
