from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.graph_edge_dto_kind import GraphEdgeDTOKind
from ..types import UNSET, Unset

T = TypeVar("T", bound="GraphEdgeDTO")


@_attrs_define
class GraphEdgeDTO:
    """One transition record.  ``id`` is derived from artifact content, so it
    is stable across recompiles that do not change the transition, and unique
    within the artifact: ``f"{rule_id}::{source}::{outcome}"``.

        Attributes:
            id (str):
            rule_id (str):
            source (str):
            source_port (str):
            target (str):
            outcome (str):
            label (str):
            kind (GraphEdgeDTOKind):
            reserved (bool | Unset):  Default: False.
            condition (None | str | Unset):
    """

    id: str
    rule_id: str
    source: str
    source_port: str
    target: str
    outcome: str
    label: str
    kind: GraphEdgeDTOKind
    reserved: bool | Unset = False
    condition: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        rule_id = self.rule_id

        source = self.source

        source_port = self.source_port

        target = self.target

        outcome = self.outcome

        label = self.label

        kind = self.kind.value

        reserved = self.reserved

        condition: None | str | Unset
        if isinstance(self.condition, Unset):
            condition = UNSET
        else:
            condition = self.condition

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "id": id,
                "rule_id": rule_id,
                "source": source,
                "source_port": source_port,
                "target": target,
                "outcome": outcome,
                "label": label,
                "kind": kind,
            }
        )
        if reserved is not UNSET:
            field_dict["reserved"] = reserved
        if condition is not UNSET:
            field_dict["condition"] = condition

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        rule_id = d.pop("rule_id")

        source = d.pop("source")

        source_port = d.pop("source_port")

        target = d.pop("target")

        outcome = d.pop("outcome")

        label = d.pop("label")

        kind = GraphEdgeDTOKind(d.pop("kind"))

        reserved = d.pop("reserved", UNSET)

        def _parse_condition(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        condition = _parse_condition(d.pop("condition", UNSET))

        graph_edge_dto = cls(
            id=id,
            rule_id=rule_id,
            source=source,
            source_port=source_port,
            target=target,
            outcome=outcome,
            label=label,
            kind=kind,
            reserved=reserved,
            condition=condition,
        )

        return graph_edge_dto
