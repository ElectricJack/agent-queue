from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="CollaborationCreateArgs")


@_attrs_define
class CollaborationCreateArgs:
    """
    Attributes:
        task_ids (list[str]):
        idempotency_key (str):
        project_id (None | str | Unset):
        task_id (None | str | Unset):
        session_id (None | str | Unset):
        goal (None | str | Unset):
        deadline_seconds (int | Unset):  Default: 7200.
        message_budget (int | Unset):  Default: 40.
    """

    task_ids: list[str]
    idempotency_key: str
    project_id: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    session_id: None | str | Unset = UNSET
    goal: None | str | Unset = UNSET
    deadline_seconds: int | Unset = 7200
    message_budget: int | Unset = 40

    def to_dict(self) -> dict[str, Any]:
        task_ids = self.task_ids

        idempotency_key = self.idempotency_key

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        session_id: None | str | Unset
        if isinstance(self.session_id, Unset):
            session_id = UNSET
        else:
            session_id = self.session_id

        goal: None | str | Unset
        if isinstance(self.goal, Unset):
            goal = UNSET
        else:
            goal = self.goal

        deadline_seconds = self.deadline_seconds

        message_budget = self.message_budget

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "task_ids": task_ids,
                "idempotency_key": idempotency_key,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if session_id is not UNSET:
            field_dict["session_id"] = session_id
        if goal is not UNSET:
            field_dict["goal"] = goal
        if deadline_seconds is not UNSET:
            field_dict["deadline_seconds"] = deadline_seconds
        if message_budget is not UNSET:
            field_dict["message_budget"] = message_budget

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_ids = cast(list[str], d.pop("task_ids"))

        idempotency_key = d.pop("idempotency_key")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_session_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        session_id = _parse_session_id(d.pop("session_id", UNSET))

        def _parse_goal(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        goal = _parse_goal(d.pop("goal", UNSET))

        deadline_seconds = d.pop("deadline_seconds", UNSET)

        message_budget = d.pop("message_budget", UNSET)

        collaboration_create_args = cls(
            task_ids=task_ids,
            idempotency_key=idempotency_key,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            goal=goal,
            deadline_seconds=deadline_seconds,
            message_budget=message_budget,
        )

        return collaboration_create_args
