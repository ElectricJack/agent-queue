from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="RebindWorkspaceRequest")


@_attrs_define
class RebindWorkspaceRequest:
    """
    Attributes:
        workspace_id (str): Workspace to re-bind
        task_id (str): Task holding the active claim
        session_id (str): Session holding that exact claim
    """

    workspace_id: str
    task_id: str
    session_id: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        workspace_id = self.workspace_id

        task_id = self.task_id

        session_id = self.session_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "workspace_id": workspace_id,
                "task_id": task_id,
                "session_id": session_id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        workspace_id = d.pop("workspace_id")

        task_id = d.pop("task_id")

        session_id = d.pop("session_id")

        rebind_workspace_request = cls(
            workspace_id=workspace_id,
            task_id=task_id,
            session_id=session_id,
        )

        rebind_workspace_request.additional_properties = d
        return rebind_workspace_request

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
