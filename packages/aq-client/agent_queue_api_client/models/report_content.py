from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.report_content_coverage import ReportContentCoverage
    from ..models.report_content_omitted import ReportContentOmitted
    from ..models.report_item import ReportItem
    from ..models.report_project import ReportProject


T = TypeVar("T", bound="ReportContent")


@_attrs_define
class ReportContent:
    """
    Attributes:
        version (int):
        summary (str):
        projects (list[ReportProject]):
        coverage (ReportContentCoverage):
        global_facts (list[ReportItem] | Unset):
        omitted (ReportContentOmitted | Unset):
    """

    version: int
    summary: str
    projects: list[ReportProject]
    coverage: ReportContentCoverage
    global_facts: list[ReportItem] | Unset = UNSET
    omitted: ReportContentOmitted | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        version = self.version

        summary = self.summary

        projects = []
        for projects_item_data in self.projects:
            projects_item = projects_item_data.to_dict()
            projects.append(projects_item)

        coverage = self.coverage.to_dict()

        global_facts: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.global_facts, Unset):
            global_facts = []
            for global_facts_item_data in self.global_facts:
                global_facts_item = global_facts_item_data.to_dict()
                global_facts.append(global_facts_item)

        omitted: dict[str, Any] | Unset = UNSET
        if not isinstance(self.omitted, Unset):
            omitted = self.omitted.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "version": version,
                "summary": summary,
                "projects": projects,
                "coverage": coverage,
            }
        )
        if global_facts is not UNSET:
            field_dict["global_facts"] = global_facts
        if omitted is not UNSET:
            field_dict["omitted"] = omitted

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.report_content_coverage import ReportContentCoverage
        from ..models.report_content_omitted import ReportContentOmitted
        from ..models.report_item import ReportItem
        from ..models.report_project import ReportProject

        d = dict(src_dict)
        version = d.pop("version")

        summary = d.pop("summary")

        projects = []
        _projects = d.pop("projects")
        for projects_item_data in _projects:
            projects_item = ReportProject.from_dict(projects_item_data)

            projects.append(projects_item)

        coverage = ReportContentCoverage.from_dict(d.pop("coverage"))

        _global_facts = d.pop("global_facts", UNSET)
        global_facts: list[ReportItem] | Unset = UNSET
        if _global_facts is not UNSET:
            global_facts = []
            for global_facts_item_data in _global_facts:
                global_facts_item = ReportItem.from_dict(global_facts_item_data)

                global_facts.append(global_facts_item)

        _omitted = d.pop("omitted", UNSET)
        omitted: ReportContentOmitted | Unset
        if isinstance(_omitted, Unset):
            omitted = UNSET
        else:
            omitted = ReportContentOmitted.from_dict(_omitted)

        report_content = cls(
            version=version,
            summary=summary,
            projects=projects,
            coverage=coverage,
            global_facts=global_facts,
            omitted=omitted,
        )

        report_content.additional_properties = d
        return report_content

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
