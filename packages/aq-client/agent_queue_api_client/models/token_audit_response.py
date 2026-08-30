from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.token_audit_response_by_project_item import TokenAuditResponseByProjectItem
    from ..models.token_audit_response_daily_item import TokenAuditResponseDailyItem
    from ..models.token_audit_response_top_tasks_item import TokenAuditResponseTopTasksItem


T = TypeVar("T", bound="TokenAuditResponse")


@_attrs_define
class TokenAuditResponse:
    """
    Attributes:
        total (int | Unset):  Default: 0.
        days (int | Unset):  Default: 7.
        since (str | Unset):  Default: ''.
        until (str | Unset):  Default: ''.
        project_id (None | str | Unset):
        by_project (list[TokenAuditResponseByProjectItem] | Unset):
        top_tasks (list[TokenAuditResponseTopTasksItem] | Unset):
        daily (list[TokenAuditResponseDailyItem] | Unset):
    """

    total: int | Unset = 0
    days: int | Unset = 7
    since: str | Unset = ""
    until: str | Unset = ""
    project_id: None | str | Unset = UNSET
    by_project: list[TokenAuditResponseByProjectItem] | Unset = UNSET
    top_tasks: list[TokenAuditResponseTopTasksItem] | Unset = UNSET
    daily: list[TokenAuditResponseDailyItem] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        total = self.total

        days = self.days

        since = self.since

        until = self.until

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        by_project: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.by_project, Unset):
            by_project = []
            for by_project_item_data in self.by_project:
                by_project_item = by_project_item_data.to_dict()
                by_project.append(by_project_item)

        top_tasks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.top_tasks, Unset):
            top_tasks = []
            for top_tasks_item_data in self.top_tasks:
                top_tasks_item = top_tasks_item_data.to_dict()
                top_tasks.append(top_tasks_item)

        daily: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.daily, Unset):
            daily = []
            for daily_item_data in self.daily:
                daily_item = daily_item_data.to_dict()
                daily.append(daily_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if total is not UNSET:
            field_dict["total"] = total
        if days is not UNSET:
            field_dict["days"] = days
        if since is not UNSET:
            field_dict["since"] = since
        if until is not UNSET:
            field_dict["until"] = until
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if by_project is not UNSET:
            field_dict["by_project"] = by_project
        if top_tasks is not UNSET:
            field_dict["top_tasks"] = top_tasks
        if daily is not UNSET:
            field_dict["daily"] = daily

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.token_audit_response_by_project_item import TokenAuditResponseByProjectItem
        from ..models.token_audit_response_daily_item import TokenAuditResponseDailyItem
        from ..models.token_audit_response_top_tasks_item import TokenAuditResponseTopTasksItem

        d = dict(src_dict)
        total = d.pop("total", UNSET)

        days = d.pop("days", UNSET)

        since = d.pop("since", UNSET)

        until = d.pop("until", UNSET)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        _by_project = d.pop("by_project", UNSET)
        by_project: list[TokenAuditResponseByProjectItem] | Unset = UNSET
        if _by_project is not UNSET:
            by_project = []
            for by_project_item_data in _by_project:
                by_project_item = TokenAuditResponseByProjectItem.from_dict(by_project_item_data)

                by_project.append(by_project_item)

        _top_tasks = d.pop("top_tasks", UNSET)
        top_tasks: list[TokenAuditResponseTopTasksItem] | Unset = UNSET
        if _top_tasks is not UNSET:
            top_tasks = []
            for top_tasks_item_data in _top_tasks:
                top_tasks_item = TokenAuditResponseTopTasksItem.from_dict(top_tasks_item_data)

                top_tasks.append(top_tasks_item)

        _daily = d.pop("daily", UNSET)
        daily: list[TokenAuditResponseDailyItem] | Unset = UNSET
        if _daily is not UNSET:
            daily = []
            for daily_item_data in _daily:
                daily_item = TokenAuditResponseDailyItem.from_dict(daily_item_data)

                daily.append(daily_item)

        token_audit_response = cls(
            total=total,
            days=days,
            since=since,
            until=until,
            project_id=project_id,
            by_project=by_project,
            top_tasks=top_tasks,
            daily=daily,
        )

        token_audit_response.additional_properties = d
        return token_audit_response

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
