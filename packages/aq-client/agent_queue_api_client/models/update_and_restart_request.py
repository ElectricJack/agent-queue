from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="UpdateAndRestartRequest")


@_attrs_define
class UpdateAndRestartRequest:
    """
    Attributes:
        reason (None | str | Unset): Why the update is being requested
        wait_for_tasks (bool | None | Unset): If true, pause orchestrator and wait for running tasks to complete before
            restarting (up to 5 minutes). Default: false.
    """

    reason: None | str | Unset = UNSET
    wait_for_tasks: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        wait_for_tasks: bool | None | Unset
        if isinstance(self.wait_for_tasks, Unset):
            wait_for_tasks = UNSET
        else:
            wait_for_tasks = self.wait_for_tasks

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if reason is not UNSET:
            field_dict["reason"] = reason
        if wait_for_tasks is not UNSET:
            field_dict["wait_for_tasks"] = wait_for_tasks

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        def _parse_wait_for_tasks(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        wait_for_tasks = _parse_wait_for_tasks(d.pop("wait_for_tasks", UNSET))

        update_and_restart_request = cls(
            reason=reason,
            wait_for_tasks=wait_for_tasks,
        )

        update_and_restart_request.additional_properties = d
        return update_and_restart_request

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
