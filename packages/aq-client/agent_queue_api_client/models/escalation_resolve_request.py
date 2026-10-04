from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="EscalationResolveRequest")


@_attrs_define
class EscalationResolveRequest:
    """
    Attributes:
        escalation_id (str):
        outcome (str): What was done, in the words the human will read.
        expected_revision (int | None | Unset): Optional compare-and-set fence. Omit to resolve the revision this
            incident has now.
    """

    escalation_id: str
    outcome: str
    expected_revision: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        escalation_id = self.escalation_id

        outcome = self.outcome

        expected_revision: int | None | Unset
        if isinstance(self.expected_revision, Unset):
            expected_revision = UNSET
        else:
            expected_revision = self.expected_revision

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "escalation_id": escalation_id,
                "outcome": outcome,
            }
        )
        if expected_revision is not UNSET:
            field_dict["expected_revision"] = expected_revision

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        escalation_id = d.pop("escalation_id")

        outcome = d.pop("outcome")

        def _parse_expected_revision(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        expected_revision = _parse_expected_revision(d.pop("expected_revision", UNSET))

        escalation_resolve_request = cls(
            escalation_id=escalation_id,
            outcome=outcome,
            expected_revision=expected_revision,
        )

        escalation_resolve_request.additional_properties = d
        return escalation_resolve_request

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
