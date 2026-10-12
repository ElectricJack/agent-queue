from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="BindProjectRepositoryResponse")


@_attrs_define
class BindProjectRepositoryResponse:
    """First repository binding, including the audit identity when it changes.

    Attributes:
        success (bool):
        project_id (str):
        repo_url (str):
        changed (bool):
        event_id (int | None | Unset):
    """

    success: bool
    project_id: str
    repo_url: str
    changed: bool
    event_id: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        project_id = self.project_id

        repo_url = self.repo_url

        changed = self.changed

        event_id: int | None | Unset
        if isinstance(self.event_id, Unset):
            event_id = UNSET
        else:
            event_id = self.event_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
                "project_id": project_id,
                "repo_url": repo_url,
                "changed": changed,
            }
        )
        if event_id is not UNSET:
            field_dict["event_id"] = event_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success")

        project_id = d.pop("project_id")

        repo_url = d.pop("repo_url")

        changed = d.pop("changed")

        def _parse_event_id(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        event_id = _parse_event_id(d.pop("event_id", UNSET))

        bind_project_repository_response = cls(
            success=success,
            project_id=project_id,
            repo_url=repo_url,
            changed=changed,
            event_id=event_id,
        )

        bind_project_repository_response.additional_properties = d
        return bind_project_repository_response

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
