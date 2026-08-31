from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.task_session_attempt import TaskSessionAttempt


T = TypeVar("T", bound="TaskSessionsResponse")


@_attrs_define
class TaskSessionsResponse:
    """
    Attributes:
        task_id (str):
        sessions (list[TaskSessionAttempt]):
    """

    task_id: str
    sessions: list[TaskSessionAttempt]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        sessions = []
        for sessions_item_data in self.sessions:
            sessions_item = sessions_item_data.to_dict()
            sessions.append(sessions_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "sessions": sessions,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_session_attempt import TaskSessionAttempt

        d = dict(src_dict)
        task_id = d.pop("task_id")

        sessions = []
        _sessions = d.pop("sessions")
        for sessions_item_data in _sessions:
            sessions_item = TaskSessionAttempt.from_dict(sessions_item_data)

            sessions.append(sessions_item)

        task_sessions_response = cls(
            task_id=task_id,
            sessions=sessions,
        )

        task_sessions_response.additional_properties = d
        return task_sessions_response

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
