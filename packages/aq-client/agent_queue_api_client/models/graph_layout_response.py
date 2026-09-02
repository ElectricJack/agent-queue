from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.graph_layout_response_jobs_type_0_item import GraphLayoutResponseJobsType0Item
    from ..models.graph_layout_response_versions_type_0 import GraphLayoutResponseVersionsType0


T = TypeVar("T", bound="GraphLayoutResponse")


@_attrs_define
class GraphLayoutResponse:
    """Response for ``graph_layout_rebuild`` / ``graph_tidy`` (spatial-layout design §5.6, §10).

    Attributes:
        success (bool):
        project_id (None | str | Unset):
        versions (GraphLayoutResponseVersionsType0 | None | Unset):
        jobs (list[GraphLayoutResponseJobsType0Item] | None | Unset):
        error (None | str | Unset):
    """

    success: bool
    project_id: None | str | Unset = UNSET
    versions: GraphLayoutResponseVersionsType0 | None | Unset = UNSET
    jobs: list[GraphLayoutResponseJobsType0Item] | None | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.graph_layout_response_versions_type_0 import GraphLayoutResponseVersionsType0  # noqa: PLC0415

        success = self.success

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        versions: dict[str, Any] | None | Unset
        if isinstance(self.versions, Unset):
            versions = UNSET
        elif isinstance(self.versions, GraphLayoutResponseVersionsType0):
            versions = self.versions.to_dict()
        else:
            versions = self.versions

        jobs: list[dict[str, Any]] | None | Unset
        if isinstance(self.jobs, Unset):
            jobs = UNSET
        elif isinstance(self.jobs, list):
            jobs = []
            for jobs_type_0_item_data in self.jobs:
                jobs_type_0_item = jobs_type_0_item_data.to_dict()
                jobs.append(jobs_type_0_item)

        else:
            jobs = self.jobs

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if versions is not UNSET:
            field_dict["versions"] = versions
        if jobs is not UNSET:
            field_dict["jobs"] = jobs
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.graph_layout_response_jobs_type_0_item import GraphLayoutResponseJobsType0Item  # noqa: PLC0415
        from ..models.graph_layout_response_versions_type_0 import GraphLayoutResponseVersionsType0  # noqa: PLC0415

        d = dict(src_dict)
        success = d.pop("success")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_versions(data: object) -> GraphLayoutResponseVersionsType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                versions_type_0 = GraphLayoutResponseVersionsType0.from_dict(data)

                return versions_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(GraphLayoutResponseVersionsType0 | None | Unset, data)

        versions = _parse_versions(d.pop("versions", UNSET))

        def _parse_jobs(data: object) -> list[GraphLayoutResponseJobsType0Item] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                jobs_type_0 = []
                _jobs_type_0 = data
                for jobs_type_0_item_data in _jobs_type_0:
                    jobs_type_0_item = GraphLayoutResponseJobsType0Item.from_dict(jobs_type_0_item_data)

                    jobs_type_0.append(jobs_type_0_item)

                return jobs_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[GraphLayoutResponseJobsType0Item] | None | Unset, data)

        jobs = _parse_jobs(d.pop("jobs", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        graph_layout_response = cls(
            success=success,
            project_id=project_id,
            versions=versions,
            jobs=jobs,
            error=error,
        )

        graph_layout_response.additional_properties = d
        return graph_layout_response

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
