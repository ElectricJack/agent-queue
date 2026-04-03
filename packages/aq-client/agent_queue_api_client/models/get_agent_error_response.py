from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GetAgentErrorResponse")


@_attrs_define
class GetAgentErrorResponse:
    """
    Attributes:
        task_id (str):
        title (str | Unset):  Default: ''.
        status (str | Unset):  Default: ''.
        retries (str | Unset):  Default: ''.
        message (None | str | Unset):
        result (None | str | Unset):
        error_type (None | str | Unset):
        error_message (None | str | Unset):
        suggested_fix (None | str | Unset):
        agent_summary (None | str | Unset):
    """

    task_id: str
    title: str | Unset = ""
    status: str | Unset = ""
    retries: str | Unset = ""
    message: None | str | Unset = UNSET
    result: None | str | Unset = UNSET
    error_type: None | str | Unset = UNSET
    error_message: None | str | Unset = UNSET
    suggested_fix: None | str | Unset = UNSET
    agent_summary: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        title = self.title

        status = self.status

        retries = self.retries

        message: None | str | Unset
        if isinstance(self.message, Unset):
            message = UNSET
        else:
            message = self.message

        result: None | str | Unset
        if isinstance(self.result, Unset):
            result = UNSET
        else:
            result = self.result

        error_type: None | str | Unset
        if isinstance(self.error_type, Unset):
            error_type = UNSET
        else:
            error_type = self.error_type

        error_message: None | str | Unset
        if isinstance(self.error_message, Unset):
            error_message = UNSET
        else:
            error_message = self.error_message

        suggested_fix: None | str | Unset
        if isinstance(self.suggested_fix, Unset):
            suggested_fix = UNSET
        else:
            suggested_fix = self.suggested_fix

        agent_summary: None | str | Unset
        if isinstance(self.agent_summary, Unset):
            agent_summary = UNSET
        else:
            agent_summary = self.agent_summary

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
            }
        )
        if title is not UNSET:
            field_dict["title"] = title
        if status is not UNSET:
            field_dict["status"] = status
        if retries is not UNSET:
            field_dict["retries"] = retries
        if message is not UNSET:
            field_dict["message"] = message
        if result is not UNSET:
            field_dict["result"] = result
        if error_type is not UNSET:
            field_dict["error_type"] = error_type
        if error_message is not UNSET:
            field_dict["error_message"] = error_message
        if suggested_fix is not UNSET:
            field_dict["suggested_fix"] = suggested_fix
        if agent_summary is not UNSET:
            field_dict["agent_summary"] = agent_summary

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        title = d.pop("title", UNSET)

        status = d.pop("status", UNSET)

        retries = d.pop("retries", UNSET)

        def _parse_message(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        message = _parse_message(d.pop("message", UNSET))

        def _parse_result(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        result = _parse_result(d.pop("result", UNSET))

        def _parse_error_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_type = _parse_error_type(d.pop("error_type", UNSET))

        def _parse_error_message(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_message = _parse_error_message(d.pop("error_message", UNSET))

        def _parse_suggested_fix(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        suggested_fix = _parse_suggested_fix(d.pop("suggested_fix", UNSET))

        def _parse_agent_summary(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        agent_summary = _parse_agent_summary(d.pop("agent_summary", UNSET))

        get_agent_error_response = cls(
            task_id=task_id,
            title=title,
            status=status,
            retries=retries,
            message=message,
            result=result,
            error_type=error_type,
            error_message=error_message,
            suggested_fix=suggested_fix,
            agent_summary=agent_summary,
        )

        get_agent_error_response.additional_properties = d
        return get_agent_error_response

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
