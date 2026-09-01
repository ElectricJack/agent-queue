from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.stub_scan_project import StubScanProject
    from ..models.stub_scan_totals import StubScanTotals


T = TypeVar("T", bound="ScanStubStalenessResponse")


@_attrs_define
class ScanStubStalenessResponse:
    """
    Attributes:
        projects (list[StubScanProject] | Unset):
        totals (None | StubScanTotals | Unset):
        summary (str | Unset):  Default: ''.
    """

    projects: list[StubScanProject] | Unset = UNSET
    totals: None | StubScanTotals | Unset = UNSET
    summary: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.stub_scan_totals import StubScanTotals  # noqa: PLC0415

        projects: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.projects, Unset):
            projects = []
            for projects_item_data in self.projects:
                projects_item = projects_item_data.to_dict()
                projects.append(projects_item)

        totals: dict[str, Any] | None | Unset
        if isinstance(self.totals, Unset):
            totals = UNSET
        elif isinstance(self.totals, StubScanTotals):
            totals = self.totals.to_dict()
        else:
            totals = self.totals

        summary = self.summary

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if projects is not UNSET:
            field_dict["projects"] = projects
        if totals is not UNSET:
            field_dict["totals"] = totals
        if summary is not UNSET:
            field_dict["summary"] = summary

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.stub_scan_project import StubScanProject  # noqa: PLC0415
        from ..models.stub_scan_totals import StubScanTotals  # noqa: PLC0415

        d = dict(src_dict)
        _projects = d.pop("projects", UNSET)
        projects: list[StubScanProject] | Unset = UNSET
        if _projects is not UNSET:
            projects = []
            for projects_item_data in _projects:
                projects_item = StubScanProject.from_dict(projects_item_data)

                projects.append(projects_item)

        def _parse_totals(data: object) -> None | StubScanTotals | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                totals_type_0 = StubScanTotals.from_dict(data)

                return totals_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | StubScanTotals | Unset, data)

        totals = _parse_totals(d.pop("totals", UNSET))

        summary = d.pop("summary", UNSET)

        scan_stub_staleness_response = cls(
            projects=projects,
            totals=totals,
            summary=summary,
        )

        scan_stub_staleness_response.additional_properties = d
        return scan_stub_staleness_response

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
