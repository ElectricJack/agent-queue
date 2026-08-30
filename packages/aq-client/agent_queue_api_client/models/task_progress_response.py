from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="TaskProgressResponse")


@_attrs_define
class TaskProgressResponse:
    """
    Attributes:
        success (bool):
        parent_id (str):
        total (int):
        done (int):
        ready (int):
        blocked (int):
        in_progress (int):
        waves (list[list[str]]):
        max_parallelism (int):
        depth (int):
    """

    success: bool
    parent_id: str
    total: int
    done: int
    ready: int
    blocked: int
    in_progress: int
    waves: list[list[str]]
    max_parallelism: int
    depth: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        parent_id = self.parent_id

        total = self.total

        done = self.done

        ready = self.ready

        blocked = self.blocked

        in_progress = self.in_progress

        waves = []
        for waves_item_data in self.waves:
            waves_item = waves_item_data

            waves.append(waves_item)

        max_parallelism = self.max_parallelism

        depth = self.depth

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
                "parent_id": parent_id,
                "total": total,
                "done": done,
                "ready": ready,
                "blocked": blocked,
                "in_progress": in_progress,
                "waves": waves,
                "max_parallelism": max_parallelism,
                "depth": depth,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success")

        parent_id = d.pop("parent_id")

        total = d.pop("total")

        done = d.pop("done")

        ready = d.pop("ready")

        blocked = d.pop("blocked")

        in_progress = d.pop("in_progress")

        waves = []
        _waves = d.pop("waves")
        for waves_item_data in _waves:
            waves_item = cast(list[str], waves_item_data)

            waves.append(waves_item)

        max_parallelism = d.pop("max_parallelism")

        depth = d.pop("depth")

        task_progress_response = cls(
            success=success,
            parent_id=parent_id,
            total=total,
            done=done,
            ready=ready,
            blocked=blocked,
            in_progress=in_progress,
            waves=waves,
            max_parallelism=max_parallelism,
            depth=depth,
        )

        task_progress_response.additional_properties = d
        return task_progress_response

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
