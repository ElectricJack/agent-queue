from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PoolInstanceStatus")


@_attrs_define
class PoolInstanceStatus:
    """One active or quarantined session belonging to a worker pool.

    Attributes:
        session_id (str):
        name (str):
        state (str):
        started_at (float):
        task_id (None | str | Unset):
        task_title (None | str | Unset):
        idle_seconds (float | None | Unset):
        quarantine_reason (None | str | Unset):
    """

    session_id: str
    name: str
    state: str
    started_at: float
    task_id: None | str | Unset = UNSET
    task_title: None | str | Unset = UNSET
    idle_seconds: float | None | Unset = UNSET
    quarantine_reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        session_id = self.session_id

        name = self.name

        state = self.state

        started_at = self.started_at

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        task_title: None | str | Unset
        if isinstance(self.task_title, Unset):
            task_title = UNSET
        else:
            task_title = self.task_title

        idle_seconds: float | None | Unset
        if isinstance(self.idle_seconds, Unset):
            idle_seconds = UNSET
        else:
            idle_seconds = self.idle_seconds

        quarantine_reason: None | str | Unset
        if isinstance(self.quarantine_reason, Unset):
            quarantine_reason = UNSET
        else:
            quarantine_reason = self.quarantine_reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "session_id": session_id,
                "name": name,
                "state": state,
                "started_at": started_at,
            }
        )
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if task_title is not UNSET:
            field_dict["task_title"] = task_title
        if idle_seconds is not UNSET:
            field_dict["idle_seconds"] = idle_seconds
        if quarantine_reason is not UNSET:
            field_dict["quarantine_reason"] = quarantine_reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        session_id = d.pop("session_id")

        name = d.pop("name")

        state = d.pop("state")

        started_at = d.pop("started_at")

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_task_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_title = _parse_task_title(d.pop("task_title", UNSET))

        def _parse_idle_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        idle_seconds = _parse_idle_seconds(d.pop("idle_seconds", UNSET))

        def _parse_quarantine_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        quarantine_reason = _parse_quarantine_reason(d.pop("quarantine_reason", UNSET))

        pool_instance_status = cls(
            session_id=session_id,
            name=name,
            state=state,
            started_at=started_at,
            task_id=task_id,
            task_title=task_title,
            idle_seconds=idle_seconds,
            quarantine_reason=quarantine_reason,
        )

        pool_instance_status.additional_properties = d
        return pool_instance_status

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
