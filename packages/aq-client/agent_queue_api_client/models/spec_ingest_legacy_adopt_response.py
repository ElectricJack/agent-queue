from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SpecIngestLegacyAdoptResponse")


@_attrs_define
class SpecIngestLegacyAdoptResponse:
    """Either the proposal is left gated, or the adopted batch's held receipt
    (``_batch_receipt`` / ``_create_ingest_batch``) is returned, so
    ``extra="allow"`` keeps the receipt fields without pinning one shape.

        Attributes:
            success (bool | Unset):  Default: True.
            proposal_id (None | str | Unset):
            left_gated (bool | None | Unset):
            batch_id (None | str | Unset):
    """

    success: bool | Unset = True
    proposal_id: None | str | Unset = UNSET
    left_gated: bool | None | Unset = UNSET
    batch_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        proposal_id: None | str | Unset
        if isinstance(self.proposal_id, Unset):
            proposal_id = UNSET
        else:
            proposal_id = self.proposal_id

        left_gated: bool | None | Unset
        if isinstance(self.left_gated, Unset):
            left_gated = UNSET
        else:
            left_gated = self.left_gated

        batch_id: None | str | Unset
        if isinstance(self.batch_id, Unset):
            batch_id = UNSET
        else:
            batch_id = self.batch_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if proposal_id is not UNSET:
            field_dict["proposal_id"] = proposal_id
        if left_gated is not UNSET:
            field_dict["left_gated"] = left_gated
        if batch_id is not UNSET:
            field_dict["batch_id"] = batch_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success", UNSET)

        def _parse_proposal_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        proposal_id = _parse_proposal_id(d.pop("proposal_id", UNSET))

        def _parse_left_gated(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        left_gated = _parse_left_gated(d.pop("left_gated", UNSET))

        def _parse_batch_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        batch_id = _parse_batch_id(d.pop("batch_id", UNSET))

        spec_ingest_legacy_adopt_response = cls(
            success=success,
            proposal_id=proposal_id,
            left_gated=left_gated,
            batch_id=batch_id,
        )

        spec_ingest_legacy_adopt_response.additional_properties = d
        return spec_ingest_legacy_adopt_response

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
