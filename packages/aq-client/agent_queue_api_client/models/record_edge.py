from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.record_edge_domain import RecordEdgeDomain
from ..types import UNSET, Unset

T = TypeVar("T", bound="RecordEdge")


@_attrs_define
class RecordEdge:
    """
    Attributes:
        edge_id (str):
        source_record_id (str):
        target_record_id (str):
        domain (RecordEdgeDomain):
        type_ (str):
        target_revision_id (None | str | Unset):
        availability (str | Unset):  Default: 'available'.
    """

    edge_id: str
    source_record_id: str
    target_record_id: str
    domain: RecordEdgeDomain
    type_: str
    target_revision_id: None | str | Unset = UNSET
    availability: str | Unset = "available"
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        edge_id = self.edge_id

        source_record_id = self.source_record_id

        target_record_id = self.target_record_id

        domain = self.domain.value

        type_ = self.type_

        target_revision_id: None | str | Unset
        if isinstance(self.target_revision_id, Unset):
            target_revision_id = UNSET
        else:
            target_revision_id = self.target_revision_id

        availability = self.availability

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "edge_id": edge_id,
                "source_record_id": source_record_id,
                "target_record_id": target_record_id,
                "domain": domain,
                "type": type_,
            }
        )
        if target_revision_id is not UNSET:
            field_dict["target_revision_id"] = target_revision_id
        if availability is not UNSET:
            field_dict["availability"] = availability

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        edge_id = d.pop("edge_id")

        source_record_id = d.pop("source_record_id")

        target_record_id = d.pop("target_record_id")

        domain = RecordEdgeDomain(d.pop("domain"))

        type_ = d.pop("type")

        def _parse_target_revision_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        target_revision_id = _parse_target_revision_id(d.pop("target_revision_id", UNSET))

        availability = d.pop("availability", UNSET)

        record_edge = cls(
            edge_id=edge_id,
            source_record_id=source_record_id,
            target_record_id=target_record_id,
            domain=domain,
            type_=type_,
            target_revision_id=target_revision_id,
            availability=availability,
        )

        record_edge.additional_properties = d
        return record_edge

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
