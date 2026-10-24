from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="ScheduleRegisterArgs")


@_attrs_define
class ScheduleRegisterArgs:
    """
    Attributes:
        project_id (str):
        body (str):
        due_at (str):
        idempotency_key (str):
    """

    project_id: str
    body: str
    due_at: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        body = self.body

        due_at = self.due_at

        idempotency_key = self.idempotency_key

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "project_id": project_id,
                "body": body,
                "due_at": due_at,
                "idempotency_key": idempotency_key,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        body = d.pop("body")

        due_at = d.pop("due_at")

        idempotency_key = d.pop("idempotency_key")

        schedule_register_args = cls(
            project_id=project_id,
            body=body,
            due_at=due_at,
            idempotency_key=idempotency_key,
        )

        return schedule_register_args
