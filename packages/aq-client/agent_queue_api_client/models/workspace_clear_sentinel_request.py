from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WorkspaceClearSentinelRequest")


@_attrs_define
class WorkspaceClearSentinelRequest:
    """
    Attributes:
        workspace_id (str): Slot workspace id whose sentinel to clear.
        if_task_terminal (bool | Unset): Clear only when the named task is terminal (default true; the only supported
            mode). Default: True.
    """

    workspace_id: str
    if_task_terminal: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        workspace_id = self.workspace_id

        if_task_terminal = self.if_task_terminal

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "workspace_id": workspace_id,
            }
        )
        if if_task_terminal is not UNSET:
            field_dict["if_task_terminal"] = if_task_terminal

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        workspace_id = d.pop("workspace_id")

        if_task_terminal = d.pop("if_task_terminal", UNSET)

        workspace_clear_sentinel_request = cls(
            workspace_id=workspace_id,
            if_task_terminal=if_task_terminal,
        )

        workspace_clear_sentinel_request.additional_properties = d
        return workspace_clear_sentinel_request

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
