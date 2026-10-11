from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.operator_execute_request_arguments_type_0 import OperatorExecuteRequestArgumentsType0


T = TypeVar("T", bound="OperatorExecuteRequest")


@_attrs_define
class OperatorExecuteRequest:
    """
    Attributes:
        command (str):
        arguments (None | OperatorExecuteRequestArgumentsType0 | Unset):
        decision_id (None | str | Unset):
        project_id (None | str | Unset):
        task_id (None | str | Unset):
        session_id (None | str | Unset):
        preflight_id (int | None | Unset):
    """

    command: str
    arguments: None | OperatorExecuteRequestArgumentsType0 | Unset = UNSET
    decision_id: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    session_id: None | str | Unset = UNSET
    preflight_id: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.operator_execute_request_arguments_type_0 import OperatorExecuteRequestArgumentsType0

        command = self.command

        arguments: dict[str, Any] | None | Unset
        if isinstance(self.arguments, Unset):
            arguments = UNSET
        elif isinstance(self.arguments, OperatorExecuteRequestArgumentsType0):
            arguments = self.arguments.to_dict()
        else:
            arguments = self.arguments

        decision_id: None | str | Unset
        if isinstance(self.decision_id, Unset):
            decision_id = UNSET
        else:
            decision_id = self.decision_id

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

        preflight_id: int | None | Unset
        if isinstance(self.preflight_id, Unset):
            preflight_id = UNSET
        else:
            preflight_id = self.preflight_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "command": command,
            }
        )
        if arguments is not UNSET:
            field_dict["arguments"] = arguments
        if decision_id is not UNSET:
            field_dict["decision_id"] = decision_id
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if session_id is not UNSET:
            field_dict["session_id"] = session_id
        if preflight_id is not UNSET:
            field_dict["preflight_id"] = preflight_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operator_execute_request_arguments_type_0 import OperatorExecuteRequestArgumentsType0

        d = dict(src_dict)
        command = d.pop("command")

        def _parse_arguments(data: object) -> None | OperatorExecuteRequestArgumentsType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                arguments_type_0 = OperatorExecuteRequestArgumentsType0.from_dict(data)

                return arguments_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OperatorExecuteRequestArgumentsType0 | Unset, data)

        arguments = _parse_arguments(d.pop("arguments", UNSET))

        def _parse_decision_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        decision_id = _parse_decision_id(d.pop("decision_id", UNSET))

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

        def _parse_preflight_id(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        preflight_id = _parse_preflight_id(d.pop("preflight_id", UNSET))

        operator_execute_request = cls(
            command=command,
            arguments=arguments,
            decision_id=decision_id,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            preflight_id=preflight_id,
        )

        operator_execute_request.additional_properties = d
        return operator_execute_request

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
