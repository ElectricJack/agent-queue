from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DecisionRecordRequest")


@_attrs_define
class DecisionRecordRequest:
    """
    Attributes:
        object_kind (str):
        object_id (str):
        effect (str):
        operator (str):
        decision (str):
        source (str):
        source_ref (str):
        idempotency_key (str):
        releases (None | str | Unset):
    """

    object_kind: str
    object_id: str
    effect: str
    operator: str
    decision: str
    source: str
    source_ref: str
    idempotency_key: str
    releases: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        object_kind = self.object_kind

        object_id = self.object_id

        effect = self.effect

        operator = self.operator

        decision = self.decision

        source = self.source

        source_ref = self.source_ref

        idempotency_key = self.idempotency_key

        releases: None | str | Unset
        if isinstance(self.releases, Unset):
            releases = UNSET
        else:
            releases = self.releases

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "object_kind": object_kind,
                "object_id": object_id,
                "effect": effect,
                "operator": operator,
                "decision": decision,
                "source": source,
                "source_ref": source_ref,
                "idempotency_key": idempotency_key,
            }
        )
        if releases is not UNSET:
            field_dict["releases"] = releases

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        object_kind = d.pop("object_kind")

        object_id = d.pop("object_id")

        effect = d.pop("effect")

        operator = d.pop("operator")

        decision = d.pop("decision")

        source = d.pop("source")

        source_ref = d.pop("source_ref")

        idempotency_key = d.pop("idempotency_key")

        def _parse_releases(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        releases = _parse_releases(d.pop("releases", UNSET))

        decision_record_request = cls(
            object_kind=object_kind,
            object_id=object_id,
            effect=effect,
            operator=operator,
            decision=decision,
            source=source,
            source_ref=source_ref,
            idempotency_key=idempotency_key,
            releases=releases,
        )

        decision_record_request.additional_properties = d
        return decision_record_request

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
