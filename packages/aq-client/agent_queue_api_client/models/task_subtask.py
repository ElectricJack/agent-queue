from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskSubtask")


@_attrs_define
class TaskSubtask:
    """One durable checklist row under a task (``task_subtasks`` table).

    Attributes:
        id (str):
        task_id (str):
        project_id (str):
        ordinal (int):
        title (str):
        status (str):
        created_at (float):
        updated_at (float):
        note (None | str | Unset):
    """

    id: str
    task_id: str
    project_id: str
    ordinal: int
    title: str
    status: str
    created_at: float
    updated_at: float
    note: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        task_id = self.task_id

        project_id = self.project_id

        ordinal = self.ordinal

        title = self.title

        status = self.status

        created_at = self.created_at

        updated_at = self.updated_at

        note: None | str | Unset
        if isinstance(self.note, Unset):
            note = UNSET
        else:
            note = self.note

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "task_id": task_id,
                "project_id": project_id,
                "ordinal": ordinal,
                "title": title,
                "status": status,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )
        if note is not UNSET:
            field_dict["note"] = note

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        task_id = d.pop("task_id")

        project_id = d.pop("project_id")

        ordinal = d.pop("ordinal")

        title = d.pop("title")

        status = d.pop("status")

        created_at = d.pop("created_at")

        updated_at = d.pop("updated_at")

        def _parse_note(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        note = _parse_note(d.pop("note", UNSET))

        task_subtask = cls(
            id=id,
            task_id=task_id,
            project_id=project_id,
            ordinal=ordinal,
            title=title,
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            note=note,
        )

        task_subtask.additional_properties = d
        return task_subtask

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
