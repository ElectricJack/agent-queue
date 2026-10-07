from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="UpdateAndRestartResponse")


@_attrs_define
class UpdateAndRestartResponse:
    """
    Attributes:
        status (str | Unset):  Default: 'updating'.
        message (str | Unset):  Default: ''.
        pull_output (str | Unset):  Default: ''.
        reason (str | Unset):  Default: ''.
        waited_for_tasks (bool | Unset):  Default: False.
        pid (int | None | Unset):
        log (None | str | Unset):
        selector (None | str | Unset):
        commit (None | str | Unset):
    """

    status: str | Unset = "updating"
    message: str | Unset = ""
    pull_output: str | Unset = ""
    reason: str | Unset = ""
    waited_for_tasks: bool | Unset = False
    pid: int | None | Unset = UNSET
    log: None | str | Unset = UNSET
    selector: None | str | Unset = UNSET
    commit: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        status = self.status

        message = self.message

        pull_output = self.pull_output

        reason = self.reason

        waited_for_tasks = self.waited_for_tasks

        pid: int | None | Unset
        if isinstance(self.pid, Unset):
            pid = UNSET
        else:
            pid = self.pid

        log: None | str | Unset
        if isinstance(self.log, Unset):
            log = UNSET
        else:
            log = self.log

        selector: None | str | Unset
        if isinstance(self.selector, Unset):
            selector = UNSET
        else:
            selector = self.selector

        commit: None | str | Unset
        if isinstance(self.commit, Unset):
            commit = UNSET
        else:
            commit = self.commit

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if status is not UNSET:
            field_dict["status"] = status
        if message is not UNSET:
            field_dict["message"] = message
        if pull_output is not UNSET:
            field_dict["pull_output"] = pull_output
        if reason is not UNSET:
            field_dict["reason"] = reason
        if waited_for_tasks is not UNSET:
            field_dict["waited_for_tasks"] = waited_for_tasks
        if pid is not UNSET:
            field_dict["pid"] = pid
        if log is not UNSET:
            field_dict["log"] = log
        if selector is not UNSET:
            field_dict["selector"] = selector
        if commit is not UNSET:
            field_dict["commit"] = commit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        status = d.pop("status", UNSET)

        message = d.pop("message", UNSET)

        pull_output = d.pop("pull_output", UNSET)

        reason = d.pop("reason", UNSET)

        waited_for_tasks = d.pop("waited_for_tasks", UNSET)

        def _parse_pid(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        pid = _parse_pid(d.pop("pid", UNSET))

        def _parse_log(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        log = _parse_log(d.pop("log", UNSET))

        def _parse_selector(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        selector = _parse_selector(d.pop("selector", UNSET))

        def _parse_commit(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        commit = _parse_commit(d.pop("commit", UNSET))

        update_and_restart_response = cls(
            status=status,
            message=message,
            pull_output=pull_output,
            reason=reason,
            waited_for_tasks=waited_for_tasks,
            pid=pid,
            log=log,
            selector=selector,
            commit=commit,
        )

        update_and_restart_response.additional_properties = d
        return update_and_restart_response

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
