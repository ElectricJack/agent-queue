from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="RecordFileInspectionRequest")


@_attrs_define
class RecordFileInspectionRequest:
    """
    Attributes:
        project_id (str): Project ID under which to record the inspection.
        file_path (str): Relative or absolute path of the inspected file.
        summary (None | str | Unset): Optional short summary of the inspection outcome.
        findings_count (int | None | Unset): Optional number of findings produced by the inspection.
        category (None | str | Unset): Optional category label (e.g. 'source', 'specs', 'tests', 'config', 'recent') for
            reporting.
    """

    project_id: str
    file_path: str
    summary: None | str | Unset = UNSET
    findings_count: int | None | Unset = UNSET
    category: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        file_path = self.file_path

        summary: None | str | Unset
        if isinstance(self.summary, Unset):
            summary = UNSET
        else:
            summary = self.summary

        findings_count: int | None | Unset
        if isinstance(self.findings_count, Unset):
            findings_count = UNSET
        else:
            findings_count = self.findings_count

        category: None | str | Unset
        if isinstance(self.category, Unset):
            category = UNSET
        else:
            category = self.category

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "file_path": file_path,
            }
        )
        if summary is not UNSET:
            field_dict["summary"] = summary
        if findings_count is not UNSET:
            field_dict["findings_count"] = findings_count
        if category is not UNSET:
            field_dict["category"] = category

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        file_path = d.pop("file_path")

        def _parse_summary(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        summary = _parse_summary(d.pop("summary", UNSET))

        def _parse_findings_count(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        findings_count = _parse_findings_count(d.pop("findings_count", UNSET))

        def _parse_category(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        category = _parse_category(d.pop("category", UNSET))

        record_file_inspection_request = cls(
            project_id=project_id,
            file_path=file_path,
            summary=summary,
            findings_count=findings_count,
            category=category,
        )

        record_file_inspection_request.additional_properties = d
        return record_file_inspection_request

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
