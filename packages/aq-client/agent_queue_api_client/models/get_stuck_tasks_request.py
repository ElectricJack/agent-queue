from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GetStuckTasksRequest")


@_attrs_define
class GetStuckTasksRequest:
    """
    Attributes:
        assigned_threshold_seconds (int | Unset): Max seconds a task may stay ASSIGNED before being flagged as stuck.
            Default 1800 (30 minutes). Default: 1800.
        in_progress_threshold_seconds (int | Unset): Max seconds a task may stay IN_PROGRESS before being flagged as
            stuck.  Default 7200 (2 hours). Default: 7200.
        now (float | None | Unset): Reference Unix timestamp (seconds since epoch).  Playbooks should pass the trigger
            event's ``tick_time`` so repeated runs are deterministic.  Defaults to the server's current time.
        project_id (None | str | Unset): Optional project filter.  When omitted, all projects are scanned.
        pagination (None | str | Unset):
        snapshot_id (None | str | Unset):
        page_token (None | str | Unset):
        page_size (int | None | Unset):
    """

    assigned_threshold_seconds: int | Unset = 1800
    in_progress_threshold_seconds: int | Unset = 7200
    now: float | None | Unset = UNSET
    project_id: None | str | Unset = UNSET
    pagination: None | str | Unset = UNSET
    snapshot_id: None | str | Unset = UNSET
    page_token: None | str | Unset = UNSET
    page_size: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        assigned_threshold_seconds = self.assigned_threshold_seconds

        in_progress_threshold_seconds = self.in_progress_threshold_seconds

        now: float | None | Unset
        if isinstance(self.now, Unset):
            now = UNSET
        else:
            now = self.now

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        pagination: None | str | Unset
        if isinstance(self.pagination, Unset):
            pagination = UNSET
        else:
            pagination = self.pagination

        snapshot_id: None | str | Unset
        if isinstance(self.snapshot_id, Unset):
            snapshot_id = UNSET
        else:
            snapshot_id = self.snapshot_id

        page_token: None | str | Unset
        if isinstance(self.page_token, Unset):
            page_token = UNSET
        else:
            page_token = self.page_token

        page_size: int | None | Unset
        if isinstance(self.page_size, Unset):
            page_size = UNSET
        else:
            page_size = self.page_size

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if assigned_threshold_seconds is not UNSET:
            field_dict["assigned_threshold_seconds"] = assigned_threshold_seconds
        if in_progress_threshold_seconds is not UNSET:
            field_dict["in_progress_threshold_seconds"] = in_progress_threshold_seconds
        if now is not UNSET:
            field_dict["now"] = now
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if pagination is not UNSET:
            field_dict["pagination"] = pagination
        if snapshot_id is not UNSET:
            field_dict["snapshot_id"] = snapshot_id
        if page_token is not UNSET:
            field_dict["page_token"] = page_token
        if page_size is not UNSET:
            field_dict["page_size"] = page_size

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        assigned_threshold_seconds = d.pop("assigned_threshold_seconds", UNSET)

        in_progress_threshold_seconds = d.pop("in_progress_threshold_seconds", UNSET)

        def _parse_now(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        now = _parse_now(d.pop("now", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_pagination(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        pagination = _parse_pagination(d.pop("pagination", UNSET))

        def _parse_snapshot_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        snapshot_id = _parse_snapshot_id(d.pop("snapshot_id", UNSET))

        def _parse_page_token(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        page_token = _parse_page_token(d.pop("page_token", UNSET))

        def _parse_page_size(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        page_size = _parse_page_size(d.pop("page_size", UNSET))

        get_stuck_tasks_request = cls(
            assigned_threshold_seconds=assigned_threshold_seconds,
            in_progress_threshold_seconds=in_progress_threshold_seconds,
            now=now,
            project_id=project_id,
            pagination=pagination,
            snapshot_id=snapshot_id,
            page_token=page_token,
            page_size=page_size,
        )

        get_stuck_tasks_request.additional_properties = d
        return get_stuck_tasks_request

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
