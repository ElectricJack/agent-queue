from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="AskLegacyResetRequest")


@_attrs_define
class AskLegacyResetRequest:
    """
    Attributes:
        project_id (str):
        apply (bool | Unset):  Default: False.
        max_rows (int | Unset):  Default: 10000.
        evidence_sha256 (None | str | Unset):
    """

    project_id: str
    apply: bool | Unset = False
    max_rows: int | Unset = 10000
    evidence_sha256: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        apply = self.apply

        max_rows = self.max_rows

        evidence_sha256: None | str | Unset
        if isinstance(self.evidence_sha256, Unset):
            evidence_sha256 = UNSET
        else:
            evidence_sha256 = self.evidence_sha256

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if apply is not UNSET:
            field_dict["apply"] = apply
        if max_rows is not UNSET:
            field_dict["max_rows"] = max_rows
        if evidence_sha256 is not UNSET:
            field_dict["evidence_sha256"] = evidence_sha256

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        apply = d.pop("apply", UNSET)

        max_rows = d.pop("max_rows", UNSET)

        def _parse_evidence_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        evidence_sha256 = _parse_evidence_sha256(d.pop("evidence_sha256", UNSET))

        ask_legacy_reset_request = cls(
            project_id=project_id,
            apply=apply,
            max_rows=max_rows,
            evidence_sha256=evidence_sha256,
        )

        ask_legacy_reset_request.additional_properties = d
        return ask_legacy_reset_request

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
