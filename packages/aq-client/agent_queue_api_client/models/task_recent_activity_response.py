from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_activity_item import TaskActivityItem
    from ..models.task_activity_model_total import TaskActivityModelTotal


T = TypeVar("T", bound="TaskRecentActivityResponse")


@_attrs_define
class TaskRecentActivityResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        since (float | Unset):  Default: 0.0.
        until (float | Unset):  Default: 0.0.
        hours (float | Unset):  Default: 24.0.
        project_id (None | str | Unset):
        items (list[TaskActivityItem] | Unset):
        total (int | Unset):  Default: 0.
        truncated (bool | Unset):  Default: False.
        by_model (list[TaskActivityModelTotal] | Unset):
    """

    success: bool | Unset = True
    since: float | Unset = 0.0
    until: float | Unset = 0.0
    hours: float | Unset = 24.0
    project_id: None | str | Unset = UNSET
    items: list[TaskActivityItem] | Unset = UNSET
    total: int | Unset = 0
    truncated: bool | Unset = False
    by_model: list[TaskActivityModelTotal] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        since = self.since

        until = self.until

        hours = self.hours

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        items: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.items, Unset):
            items = []
            for items_item_data in self.items:
                items_item = items_item_data.to_dict()
                items.append(items_item)

        total = self.total

        truncated = self.truncated

        by_model: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.by_model, Unset):
            by_model = []
            for by_model_item_data in self.by_model:
                by_model_item = by_model_item_data.to_dict()
                by_model.append(by_model_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if since is not UNSET:
            field_dict["since"] = since
        if until is not UNSET:
            field_dict["until"] = until
        if hours is not UNSET:
            field_dict["hours"] = hours
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if items is not UNSET:
            field_dict["items"] = items
        if total is not UNSET:
            field_dict["total"] = total
        if truncated is not UNSET:
            field_dict["truncated"] = truncated
        if by_model is not UNSET:
            field_dict["by_model"] = by_model

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_activity_item import TaskActivityItem
        from ..models.task_activity_model_total import TaskActivityModelTotal

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        since = d.pop("since", UNSET)

        until = d.pop("until", UNSET)

        hours = d.pop("hours", UNSET)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        _items = d.pop("items", UNSET)
        items: list[TaskActivityItem] | Unset = UNSET
        if _items is not UNSET:
            items = []
            for items_item_data in _items:
                items_item = TaskActivityItem.from_dict(items_item_data)

                items.append(items_item)

        total = d.pop("total", UNSET)

        truncated = d.pop("truncated", UNSET)

        _by_model = d.pop("by_model", UNSET)
        by_model: list[TaskActivityModelTotal] | Unset = UNSET
        if _by_model is not UNSET:
            by_model = []
            for by_model_item_data in _by_model:
                by_model_item = TaskActivityModelTotal.from_dict(by_model_item_data)

                by_model.append(by_model_item)

        task_recent_activity_response = cls(
            success=success,
            since=since,
            until=until,
            hours=hours,
            project_id=project_id,
            items=items,
            total=total,
            truncated=truncated,
            by_model=by_model,
        )

        task_recent_activity_response.additional_properties = d
        return task_recent_activity_response

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
