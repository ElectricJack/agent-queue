from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReportSubmitRequest")


@_attrs_define
class ReportSubmitRequest:
    """
    Attributes:
        request_id (str):
        brief_hash (str):
        expected_version (int):
        text (str):
        evidence_refs (list[Any] | None | Unset):
    """

    request_id: str
    brief_hash: str
    expected_version: int
    text: str
    evidence_refs: list[Any] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        request_id = self.request_id

        brief_hash = self.brief_hash

        expected_version = self.expected_version

        text = self.text

        evidence_refs: list[Any] | None | Unset
        if isinstance(self.evidence_refs, Unset):
            evidence_refs = UNSET
        elif isinstance(self.evidence_refs, list):
            evidence_refs = self.evidence_refs

        else:
            evidence_refs = self.evidence_refs

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "request_id": request_id,
                "brief_hash": brief_hash,
                "expected_version": expected_version,
                "text": text,
            }
        )
        if evidence_refs is not UNSET:
            field_dict["evidence_refs"] = evidence_refs

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        request_id = d.pop("request_id")

        brief_hash = d.pop("brief_hash")

        expected_version = d.pop("expected_version")

        text = d.pop("text")

        def _parse_evidence_refs(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                evidence_refs_type_0 = cast(list[Any], data)

                return evidence_refs_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        evidence_refs = _parse_evidence_refs(d.pop("evidence_refs", UNSET))

        report_submit_request = cls(
            request_id=request_id,
            brief_hash=brief_hash,
            expected_version=expected_version,
            text=text,
            evidence_refs=evidence_refs,
        )

        report_submit_request.additional_properties = d
        return report_submit_request

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
