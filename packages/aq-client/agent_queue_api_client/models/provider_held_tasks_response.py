from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_held_task import ProviderHeldTask
    from ..models.provider_held_tasks_response_by_kind import ProviderHeldTasksResponseByKind


T = TypeVar("T", bound="ProviderHeldTasksResponse")


@_attrs_define
class ProviderHeldTasksResponse:
    """``provider_held_tasks``: every held task, and how many per ``kind``.

    Attributes:
        now (float):
        success (bool | Unset):  Default: True.
        tasks (list[ProviderHeldTask] | Unset):
        total (int | Unset):  Default: 0.
        by_kind (ProviderHeldTasksResponseByKind | Unset):
    """

    now: float
    success: bool | Unset = True
    tasks: list[ProviderHeldTask] | Unset = UNSET
    total: int | Unset = 0
    by_kind: ProviderHeldTasksResponseByKind | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        now = self.now

        success = self.success

        tasks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.tasks, Unset):
            tasks = []
            for tasks_item_data in self.tasks:
                tasks_item = tasks_item_data.to_dict()
                tasks.append(tasks_item)

        total = self.total

        by_kind: dict[str, Any] | Unset = UNSET
        if not isinstance(self.by_kind, Unset):
            by_kind = self.by_kind.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "now": now,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if tasks is not UNSET:
            field_dict["tasks"] = tasks
        if total is not UNSET:
            field_dict["total"] = total
        if by_kind is not UNSET:
            field_dict["by_kind"] = by_kind

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_held_task import ProviderHeldTask
        from ..models.provider_held_tasks_response_by_kind import ProviderHeldTasksResponseByKind

        d = dict(src_dict)
        now = d.pop("now")

        success = d.pop("success", UNSET)

        _tasks = d.pop("tasks", UNSET)
        tasks: list[ProviderHeldTask] | Unset = UNSET
        if _tasks is not UNSET:
            tasks = []
            for tasks_item_data in _tasks:
                tasks_item = ProviderHeldTask.from_dict(tasks_item_data)

                tasks.append(tasks_item)

        total = d.pop("total", UNSET)

        _by_kind = d.pop("by_kind", UNSET)
        by_kind: ProviderHeldTasksResponseByKind | Unset
        if isinstance(_by_kind, Unset):
            by_kind = UNSET
        else:
            by_kind = ProviderHeldTasksResponseByKind.from_dict(_by_kind)

        provider_held_tasks_response = cls(
            now=now,
            success=success,
            tasks=tasks,
            total=total,
            by_kind=by_kind,
        )

        provider_held_tasks_response.additional_properties = d
        return provider_held_tasks_response

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
