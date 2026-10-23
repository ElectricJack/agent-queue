from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SpecIngestLegacyAdoptRequest")


@_attrs_define
class SpecIngestLegacyAdoptRequest:
    """
    Attributes:
        proposal_id (str):
        decision_id (None | str | Unset):
        packet_sha256 (None | str | Unset):
        leave_gated (bool | None | Unset):
    """

    proposal_id: str
    decision_id: None | str | Unset = UNSET
    packet_sha256: None | str | Unset = UNSET
    leave_gated: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        proposal_id = self.proposal_id

        decision_id: None | str | Unset
        if isinstance(self.decision_id, Unset):
            decision_id = UNSET
        else:
            decision_id = self.decision_id

        packet_sha256: None | str | Unset
        if isinstance(self.packet_sha256, Unset):
            packet_sha256 = UNSET
        else:
            packet_sha256 = self.packet_sha256

        leave_gated: bool | None | Unset
        if isinstance(self.leave_gated, Unset):
            leave_gated = UNSET
        else:
            leave_gated = self.leave_gated

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "proposal_id": proposal_id,
            }
        )
        if decision_id is not UNSET:
            field_dict["decision_id"] = decision_id
        if packet_sha256 is not UNSET:
            field_dict["packet_sha256"] = packet_sha256
        if leave_gated is not UNSET:
            field_dict["leave_gated"] = leave_gated

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        proposal_id = d.pop("proposal_id")

        def _parse_decision_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        decision_id = _parse_decision_id(d.pop("decision_id", UNSET))

        def _parse_packet_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        packet_sha256 = _parse_packet_sha256(d.pop("packet_sha256", UNSET))

        def _parse_leave_gated(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        leave_gated = _parse_leave_gated(d.pop("leave_gated", UNSET))

        spec_ingest_legacy_adopt_request = cls(
            proposal_id=proposal_id,
            decision_id=decision_id,
            packet_sha256=packet_sha256,
            leave_gated=leave_gated,
        )

        spec_ingest_legacy_adopt_request.additional_properties = d
        return spec_ingest_legacy_adopt_request

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
