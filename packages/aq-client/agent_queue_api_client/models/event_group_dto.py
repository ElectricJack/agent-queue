from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="EventGroupDTO")


@_attrs_define
class EventGroupDTO:
    """
    Attributes:
        event_type (str):
        rule_ids (list[str] | Unset):
        node_count (int | Unset):  Default: 0.
        edge_count (int | Unset):  Default: 0.
    """

    event_type: str
    rule_ids: list[str] | Unset = UNSET
    node_count: int | Unset = 0
    edge_count: int | Unset = 0

    def to_dict(self) -> dict[str, Any]:
        event_type = self.event_type

        rule_ids: list[str] | Unset = UNSET
        if not isinstance(self.rule_ids, Unset):
            rule_ids = self.rule_ids

        node_count = self.node_count

        edge_count = self.edge_count

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "event_type": event_type,
            }
        )
        if rule_ids is not UNSET:
            field_dict["rule_ids"] = rule_ids
        if node_count is not UNSET:
            field_dict["node_count"] = node_count
        if edge_count is not UNSET:
            field_dict["edge_count"] = edge_count

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        event_type = d.pop("event_type")

        rule_ids = cast(list[str], d.pop("rule_ids", UNSET))

        node_count = d.pop("node_count", UNSET)

        edge_count = d.pop("edge_count", UNSET)

        event_group_dto = cls(
            event_type=event_type,
            rule_ids=rule_ids,
            node_count=node_count,
            edge_count=edge_count,
        )

        return event_group_dto
