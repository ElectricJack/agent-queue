from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.get_benchmark_report_request_manifest import GetBenchmarkReportRequestManifest


T = TypeVar("T", bound="GetBenchmarkReportRequest")


@_attrs_define
class GetBenchmarkReportRequest:
    """
    Attributes:
        manifest (GetBenchmarkReportRequestManifest): Version 1 cohort manifest with project_id, policy_sha256,
            rate_card_version, arms and explicit specimen/arm/attempt/task_ids pairs.
    """

    manifest: GetBenchmarkReportRequestManifest
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        manifest = self.manifest.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "manifest": manifest,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.get_benchmark_report_request_manifest import GetBenchmarkReportRequestManifest

        d = dict(src_dict)
        manifest = GetBenchmarkReportRequestManifest.from_dict(d.pop("manifest"))

        get_benchmark_report_request = cls(
            manifest=manifest,
        )

        get_benchmark_report_request.additional_properties = d
        return get_benchmark_report_request

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
