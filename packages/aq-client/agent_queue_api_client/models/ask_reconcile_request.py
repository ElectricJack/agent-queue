from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="AskReconcileRequest")


@_attrs_define
class AskReconcileRequest:
    """
    Attributes:
        project_id (None | str | Unset):
        apply (bool | Unset):  Default: False.
        limit (int | Unset):  Default: 100.
        case_id (None | str | Unset):
        disposition (None | str | Unset):
        evidence (None | str | Unset):
    """

    project_id: None | str | Unset = UNSET
    apply: bool | Unset = False
    limit: int | Unset = 100
    case_id: None | str | Unset = UNSET
    disposition: None | str | Unset = UNSET
    evidence: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        apply = self.apply

        limit = self.limit

        case_id: None | str | Unset
        if isinstance(self.case_id, Unset):
            case_id = UNSET
        else:
            case_id = self.case_id

        disposition: None | str | Unset
        if isinstance(self.disposition, Unset):
            disposition = UNSET
        else:
            disposition = self.disposition

        evidence: None | str | Unset
        if isinstance(self.evidence, Unset):
            evidence = UNSET
        else:
            evidence = self.evidence

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if apply is not UNSET:
            field_dict["apply"] = apply
        if limit is not UNSET:
            field_dict["limit"] = limit
        if case_id is not UNSET:
            field_dict["case_id"] = case_id
        if disposition is not UNSET:
            field_dict["disposition"] = disposition
        if evidence is not UNSET:
            field_dict["evidence"] = evidence

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        apply = d.pop("apply", UNSET)

        limit = d.pop("limit", UNSET)

        def _parse_case_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        case_id = _parse_case_id(d.pop("case_id", UNSET))

        def _parse_disposition(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        disposition = _parse_disposition(d.pop("disposition", UNSET))

        def _parse_evidence(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        evidence = _parse_evidence(d.pop("evidence", UNSET))

        ask_reconcile_request = cls(
            project_id=project_id,
            apply=apply,
            limit=limit,
            case_id=case_id,
            disposition=disposition,
            evidence=evidence,
        )

        ask_reconcile_request.additional_properties = d
        return ask_reconcile_request

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
