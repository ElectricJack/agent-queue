from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.pull_request_project_summary import PullRequestProjectSummary


T = TypeVar("T", bound="PullRequestProjectSummariesResponse")


@_attrs_define
class PullRequestProjectSummariesResponse:
    """
    Attributes:
        projects (list[PullRequestProjectSummary]):
        snapshot_at (float | None | Unset):
        snapshot_age_seconds (float | None | Unset):
    """

    projects: list[PullRequestProjectSummary]
    snapshot_at: float | None | Unset = UNSET
    snapshot_age_seconds: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        projects = []
        for projects_item_data in self.projects:
            projects_item = projects_item_data.to_dict()
            projects.append(projects_item)

        snapshot_at: float | None | Unset
        if isinstance(self.snapshot_at, Unset):
            snapshot_at = UNSET
        else:
            snapshot_at = self.snapshot_at

        snapshot_age_seconds: float | None | Unset
        if isinstance(self.snapshot_age_seconds, Unset):
            snapshot_age_seconds = UNSET
        else:
            snapshot_age_seconds = self.snapshot_age_seconds

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "projects": projects,
            }
        )
        if snapshot_at is not UNSET:
            field_dict["snapshot_at"] = snapshot_at
        if snapshot_age_seconds is not UNSET:
            field_dict["snapshot_age_seconds"] = snapshot_age_seconds

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.pull_request_project_summary import PullRequestProjectSummary

        d = dict(src_dict)
        projects = []
        _projects = d.pop("projects")
        for projects_item_data in _projects:
            projects_item = PullRequestProjectSummary.from_dict(projects_item_data)

            projects.append(projects_item)

        def _parse_snapshot_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        snapshot_at = _parse_snapshot_at(d.pop("snapshot_at", UNSET))

        def _parse_snapshot_age_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        snapshot_age_seconds = _parse_snapshot_age_seconds(d.pop("snapshot_age_seconds", UNSET))

        pull_request_project_summaries_response = cls(
            projects=projects,
            snapshot_at=snapshot_at,
            snapshot_age_seconds=snapshot_age_seconds,
        )

        pull_request_project_summaries_response.additional_properties = d
        return pull_request_project_summaries_response

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
