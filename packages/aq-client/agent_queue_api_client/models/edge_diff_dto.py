from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..models.edge_diff_dto_change import EdgeDiffDTOChange

T = TypeVar("T", bound="EdgeDiffDTO")


@_attrs_define
class EdgeDiffDTO:
    """
    Attributes:
        edge_id (str):
        rule_id (str):
        source (str):
        target (str):
        outcome (str):
        change (EdgeDiffDTOChange):
    """

    edge_id: str
    rule_id: str
    source: str
    target: str
    outcome: str
    change: EdgeDiffDTOChange

    def to_dict(self) -> dict[str, Any]:
        edge_id = self.edge_id

        rule_id = self.rule_id

        source = self.source

        target = self.target

        outcome = self.outcome

        change = self.change.value

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "edge_id": edge_id,
                "rule_id": rule_id,
                "source": source,
                "target": target,
                "outcome": outcome,
                "change": change,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        edge_id = d.pop("edge_id")

        rule_id = d.pop("rule_id")

        source = d.pop("source")

        target = d.pop("target")

        outcome = d.pop("outcome")

        change = EdgeDiffDTOChange(d.pop("change"))

        edge_diff_dto = cls(
            edge_id=edge_id,
            rule_id=rule_id,
            source=source,
            target=target,
            outcome=outcome,
            change=change,
        )

        return edge_diff_dto
