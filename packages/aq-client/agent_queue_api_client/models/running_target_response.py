from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="RunningTargetResponse")


@_attrs_define
class RunningTargetResponse:
    """The live leaf task a graph should navigate to, if one exists.

    ``ancestors`` is ordered from the project root to the task's immediate
    parent when a published layout is available.  It lets the root graph pan
    to the visible enclosing tile while the toolbar enters ``parent_task_id``
    to reveal the task itself.

        Attributes:
            task_id (str):
            project_id (str):
            observed_at (float):
            parent_task_id (None | str | Unset):
            ancestors (list[str] | Unset):
            layout_version (int | None | Unset):
    """

    task_id: str
    project_id: str
    observed_at: float
    parent_task_id: None | str | Unset = UNSET
    ancestors: list[str] | Unset = UNSET
    layout_version: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        project_id = self.project_id

        observed_at = self.observed_at

        parent_task_id: None | str | Unset
        if isinstance(self.parent_task_id, Unset):
            parent_task_id = UNSET
        else:
            parent_task_id = self.parent_task_id

        ancestors: list[str] | Unset = UNSET
        if not isinstance(self.ancestors, Unset):
            ancestors = self.ancestors

        layout_version: int | None | Unset
        if isinstance(self.layout_version, Unset):
            layout_version = UNSET
        else:
            layout_version = self.layout_version

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "project_id": project_id,
                "observed_at": observed_at,
            }
        )
        if parent_task_id is not UNSET:
            field_dict["parent_task_id"] = parent_task_id
        if ancestors is not UNSET:
            field_dict["ancestors"] = ancestors
        if layout_version is not UNSET:
            field_dict["layout_version"] = layout_version

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        project_id = d.pop("project_id")

        observed_at = d.pop("observed_at")

        def _parse_parent_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_task_id = _parse_parent_task_id(d.pop("parent_task_id", UNSET))

        ancestors = cast(list[str], d.pop("ancestors", UNSET))

        def _parse_layout_version(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        layout_version = _parse_layout_version(d.pop("layout_version", UNSET))

        running_target_response = cls(
            task_id=task_id,
            project_id=project_id,
            observed_at=observed_at,
            parent_task_id=parent_task_id,
            ancestors=ancestors,
            layout_version=layout_version,
        )

        running_target_response.additional_properties = d
        return running_target_response

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
