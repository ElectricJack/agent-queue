from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.list_tasks_response_artifacts_item import ListTasksResponseArtifactsItem
    from ..models.list_tasks_response_coverage_item import ListTasksResponseCoverageItem
    from ..models.list_tasks_response_identity_manifest_item import ListTasksResponseIdentityManifestItem
    from ..models.list_tasks_response_page_tokens_item import ListTasksResponsePageTokensItem
    from ..models.task_detail import TaskDetail


T = TypeVar("T", bound="ListTasksResponse")


@_attrs_define
class ListTasksResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        complete (bool | None | Unset):
        execution_status (None | str | Unset):
        error (None | str | Unset):
        error_code (None | str | Unset):
        reasons (list[str] | Unset):
        snapshot_id (None | str | Unset):
        producer (None | str | Unset):
        producer_version (int | None | Unset):
        observed_at (float | None | Unset):
        materialized_at (float | None | Unset):
        expires_at (float | None | Unset):
        interval_started_at (float | None | Unset):
        interval_finished_at (float | None | Unset):
        coverage (list[ListTasksResponseCoverageItem] | Unset):
        identity_manifest (list[ListTasksResponseIdentityManifestItem] | Unset):
        page_tokens (list[ListTasksResponsePageTokensItem] | Unset):
        artifacts (list[ListTasksResponseArtifactsItem] | Unset):
        page_count (int | Unset):  Default: 0.
        page_kind (None | str | Unset):
        page_index (int | None | Unset):
        display_mode (str | Unset):  Default: 'flat'.
        tasks (list[TaskDetail] | Unset):
        total (int | Unset):  Default: 0.
        hidden_completed (int | Unset):  Default: 0.
        filtered (bool | Unset):  Default: False.
        dependency_display (None | str | Unset):
    """

    success: bool | Unset = True
    complete: bool | None | Unset = UNSET
    execution_status: None | str | Unset = UNSET
    error: None | str | Unset = UNSET
    error_code: None | str | Unset = UNSET
    reasons: list[str] | Unset = UNSET
    snapshot_id: None | str | Unset = UNSET
    producer: None | str | Unset = UNSET
    producer_version: int | None | Unset = UNSET
    observed_at: float | None | Unset = UNSET
    materialized_at: float | None | Unset = UNSET
    expires_at: float | None | Unset = UNSET
    interval_started_at: float | None | Unset = UNSET
    interval_finished_at: float | None | Unset = UNSET
    coverage: list[ListTasksResponseCoverageItem] | Unset = UNSET
    identity_manifest: list[ListTasksResponseIdentityManifestItem] | Unset = UNSET
    page_tokens: list[ListTasksResponsePageTokensItem] | Unset = UNSET
    artifacts: list[ListTasksResponseArtifactsItem] | Unset = UNSET
    page_count: int | Unset = 0
    page_kind: None | str | Unset = UNSET
    page_index: int | None | Unset = UNSET
    display_mode: str | Unset = "flat"
    tasks: list[TaskDetail] | Unset = UNSET
    total: int | Unset = 0
    hidden_completed: int | Unset = 0
    filtered: bool | Unset = False
    dependency_display: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        complete: bool | None | Unset
        if isinstance(self.complete, Unset):
            complete = UNSET
        else:
            complete = self.complete

        execution_status: None | str | Unset
        if isinstance(self.execution_status, Unset):
            execution_status = UNSET
        else:
            execution_status = self.execution_status

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        error_code: None | str | Unset
        if isinstance(self.error_code, Unset):
            error_code = UNSET
        else:
            error_code = self.error_code

        reasons: list[str] | Unset = UNSET
        if not isinstance(self.reasons, Unset):
            reasons = self.reasons

        snapshot_id: None | str | Unset
        if isinstance(self.snapshot_id, Unset):
            snapshot_id = UNSET
        else:
            snapshot_id = self.snapshot_id

        producer: None | str | Unset
        if isinstance(self.producer, Unset):
            producer = UNSET
        else:
            producer = self.producer

        producer_version: int | None | Unset
        if isinstance(self.producer_version, Unset):
            producer_version = UNSET
        else:
            producer_version = self.producer_version

        observed_at: float | None | Unset
        if isinstance(self.observed_at, Unset):
            observed_at = UNSET
        else:
            observed_at = self.observed_at

        materialized_at: float | None | Unset
        if isinstance(self.materialized_at, Unset):
            materialized_at = UNSET
        else:
            materialized_at = self.materialized_at

        expires_at: float | None | Unset
        if isinstance(self.expires_at, Unset):
            expires_at = UNSET
        else:
            expires_at = self.expires_at

        interval_started_at: float | None | Unset
        if isinstance(self.interval_started_at, Unset):
            interval_started_at = UNSET
        else:
            interval_started_at = self.interval_started_at

        interval_finished_at: float | None | Unset
        if isinstance(self.interval_finished_at, Unset):
            interval_finished_at = UNSET
        else:
            interval_finished_at = self.interval_finished_at

        coverage: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.coverage, Unset):
            coverage = []
            for coverage_item_data in self.coverage:
                coverage_item = coverage_item_data.to_dict()
                coverage.append(coverage_item)

        identity_manifest: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.identity_manifest, Unset):
            identity_manifest = []
            for identity_manifest_item_data in self.identity_manifest:
                identity_manifest_item = identity_manifest_item_data.to_dict()
                identity_manifest.append(identity_manifest_item)

        page_tokens: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.page_tokens, Unset):
            page_tokens = []
            for page_tokens_item_data in self.page_tokens:
                page_tokens_item = page_tokens_item_data.to_dict()
                page_tokens.append(page_tokens_item)

        artifacts: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.artifacts, Unset):
            artifacts = []
            for artifacts_item_data in self.artifacts:
                artifacts_item = artifacts_item_data.to_dict()
                artifacts.append(artifacts_item)

        page_count = self.page_count

        page_kind: None | str | Unset
        if isinstance(self.page_kind, Unset):
            page_kind = UNSET
        else:
            page_kind = self.page_kind

        page_index: int | None | Unset
        if isinstance(self.page_index, Unset):
            page_index = UNSET
        else:
            page_index = self.page_index

        display_mode = self.display_mode

        tasks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.tasks, Unset):
            tasks = []
            for tasks_item_data in self.tasks:
                tasks_item = tasks_item_data.to_dict()
                tasks.append(tasks_item)

        total = self.total

        hidden_completed = self.hidden_completed

        filtered = self.filtered

        dependency_display: None | str | Unset
        if isinstance(self.dependency_display, Unset):
            dependency_display = UNSET
        else:
            dependency_display = self.dependency_display

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if complete is not UNSET:
            field_dict["complete"] = complete
        if execution_status is not UNSET:
            field_dict["execution_status"] = execution_status
        if error is not UNSET:
            field_dict["error"] = error
        if error_code is not UNSET:
            field_dict["error_code"] = error_code
        if reasons is not UNSET:
            field_dict["reasons"] = reasons
        if snapshot_id is not UNSET:
            field_dict["snapshot_id"] = snapshot_id
        if producer is not UNSET:
            field_dict["producer"] = producer
        if producer_version is not UNSET:
            field_dict["producer_version"] = producer_version
        if observed_at is not UNSET:
            field_dict["observed_at"] = observed_at
        if materialized_at is not UNSET:
            field_dict["materialized_at"] = materialized_at
        if expires_at is not UNSET:
            field_dict["expires_at"] = expires_at
        if interval_started_at is not UNSET:
            field_dict["interval_started_at"] = interval_started_at
        if interval_finished_at is not UNSET:
            field_dict["interval_finished_at"] = interval_finished_at
        if coverage is not UNSET:
            field_dict["coverage"] = coverage
        if identity_manifest is not UNSET:
            field_dict["identity_manifest"] = identity_manifest
        if page_tokens is not UNSET:
            field_dict["page_tokens"] = page_tokens
        if artifacts is not UNSET:
            field_dict["artifacts"] = artifacts
        if page_count is not UNSET:
            field_dict["page_count"] = page_count
        if page_kind is not UNSET:
            field_dict["page_kind"] = page_kind
        if page_index is not UNSET:
            field_dict["page_index"] = page_index
        if display_mode is not UNSET:
            field_dict["display_mode"] = display_mode
        if tasks is not UNSET:
            field_dict["tasks"] = tasks
        if total is not UNSET:
            field_dict["total"] = total
        if hidden_completed is not UNSET:
            field_dict["hidden_completed"] = hidden_completed
        if filtered is not UNSET:
            field_dict["filtered"] = filtered
        if dependency_display is not UNSET:
            field_dict["dependency_display"] = dependency_display

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.list_tasks_response_artifacts_item import ListTasksResponseArtifactsItem
        from ..models.list_tasks_response_coverage_item import ListTasksResponseCoverageItem
        from ..models.list_tasks_response_identity_manifest_item import ListTasksResponseIdentityManifestItem
        from ..models.list_tasks_response_page_tokens_item import ListTasksResponsePageTokensItem
        from ..models.task_detail import TaskDetail

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        def _parse_complete(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        complete = _parse_complete(d.pop("complete", UNSET))

        def _parse_execution_status(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        execution_status = _parse_execution_status(d.pop("execution_status", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        def _parse_error_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_code = _parse_error_code(d.pop("error_code", UNSET))

        reasons = cast(list[str], d.pop("reasons", UNSET))

        def _parse_snapshot_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        snapshot_id = _parse_snapshot_id(d.pop("snapshot_id", UNSET))

        def _parse_producer(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        producer = _parse_producer(d.pop("producer", UNSET))

        def _parse_producer_version(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        producer_version = _parse_producer_version(d.pop("producer_version", UNSET))

        def _parse_observed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        observed_at = _parse_observed_at(d.pop("observed_at", UNSET))

        def _parse_materialized_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        materialized_at = _parse_materialized_at(d.pop("materialized_at", UNSET))

        def _parse_expires_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        expires_at = _parse_expires_at(d.pop("expires_at", UNSET))

        def _parse_interval_started_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        interval_started_at = _parse_interval_started_at(d.pop("interval_started_at", UNSET))

        def _parse_interval_finished_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        interval_finished_at = _parse_interval_finished_at(d.pop("interval_finished_at", UNSET))

        _coverage = d.pop("coverage", UNSET)
        coverage: list[ListTasksResponseCoverageItem] | Unset = UNSET
        if _coverage is not UNSET:
            coverage = []
            for coverage_item_data in _coverage:
                coverage_item = ListTasksResponseCoverageItem.from_dict(coverage_item_data)

                coverage.append(coverage_item)

        _identity_manifest = d.pop("identity_manifest", UNSET)
        identity_manifest: list[ListTasksResponseIdentityManifestItem] | Unset = UNSET
        if _identity_manifest is not UNSET:
            identity_manifest = []
            for identity_manifest_item_data in _identity_manifest:
                identity_manifest_item = ListTasksResponseIdentityManifestItem.from_dict(identity_manifest_item_data)

                identity_manifest.append(identity_manifest_item)

        _page_tokens = d.pop("page_tokens", UNSET)
        page_tokens: list[ListTasksResponsePageTokensItem] | Unset = UNSET
        if _page_tokens is not UNSET:
            page_tokens = []
            for page_tokens_item_data in _page_tokens:
                page_tokens_item = ListTasksResponsePageTokensItem.from_dict(page_tokens_item_data)

                page_tokens.append(page_tokens_item)

        _artifacts = d.pop("artifacts", UNSET)
        artifacts: list[ListTasksResponseArtifactsItem] | Unset = UNSET
        if _artifacts is not UNSET:
            artifacts = []
            for artifacts_item_data in _artifacts:
                artifacts_item = ListTasksResponseArtifactsItem.from_dict(artifacts_item_data)

                artifacts.append(artifacts_item)

        page_count = d.pop("page_count", UNSET)

        def _parse_page_kind(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        page_kind = _parse_page_kind(d.pop("page_kind", UNSET))

        def _parse_page_index(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        page_index = _parse_page_index(d.pop("page_index", UNSET))

        display_mode = d.pop("display_mode", UNSET)

        _tasks = d.pop("tasks", UNSET)
        tasks: list[TaskDetail] | Unset = UNSET
        if _tasks is not UNSET:
            tasks = []
            for tasks_item_data in _tasks:
                tasks_item = TaskDetail.from_dict(tasks_item_data)

                tasks.append(tasks_item)

        total = d.pop("total", UNSET)

        hidden_completed = d.pop("hidden_completed", UNSET)

        filtered = d.pop("filtered", UNSET)

        def _parse_dependency_display(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        dependency_display = _parse_dependency_display(d.pop("dependency_display", UNSET))

        list_tasks_response = cls(
            success=success,
            complete=complete,
            execution_status=execution_status,
            error=error,
            error_code=error_code,
            reasons=reasons,
            snapshot_id=snapshot_id,
            producer=producer,
            producer_version=producer_version,
            observed_at=observed_at,
            materialized_at=materialized_at,
            expires_at=expires_at,
            interval_started_at=interval_started_at,
            interval_finished_at=interval_finished_at,
            coverage=coverage,
            identity_manifest=identity_manifest,
            page_tokens=page_tokens,
            artifacts=artifacts,
            page_count=page_count,
            page_kind=page_kind,
            page_index=page_index,
            display_mode=display_mode,
            tasks=tasks,
            total=total,
            hidden_completed=hidden_completed,
            filtered=filtered,
            dependency_display=dependency_display,
        )

        list_tasks_response.additional_properties = d
        return list_tasks_response

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
