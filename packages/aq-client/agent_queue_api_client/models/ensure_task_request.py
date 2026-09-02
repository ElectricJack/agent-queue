from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="EnsureTaskRequest")


@_attrs_define
class EnsureTaskRequest:
    """
    Attributes:
        project_id (str): Project ID
        dedup_key (str): Stable dedup key scoped to the project (e.g. 'triage-open'). Only one open task per
            (project_id, dedup_key) exists at a time.
        title (str): Task title (used on create)
        description (str | Unset): Task description (used on create) Default: ''.
        priority (int | Unset): Priority (lower = higher priority, default 100) Default: 100.
        profile_id (None | str | Unset): Pre-route the task to this agent profile on create. Tasks created via
            ensure_task skip triage, so the ensuring pipeline pins the executing profile directly.
        intelligence_class (None | str | Unset): Vault intelligence class for the task on create. A pinned profile is
            not a route on its own: without an explicit class the task waits for the assignment playbook to choose one. Both
            apply only when this call creates the task.
    """

    project_id: str
    dedup_key: str
    title: str
    description: str | Unset = ""
    priority: int | Unset = 100
    profile_id: None | str | Unset = UNSET
    intelligence_class: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        dedup_key = self.dedup_key

        title = self.title

        description = self.description

        priority = self.priority

        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        intelligence_class: None | str | Unset
        if isinstance(self.intelligence_class, Unset):
            intelligence_class = UNSET
        else:
            intelligence_class = self.intelligence_class

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "dedup_key": dedup_key,
                "title": title,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if priority is not UNSET:
            field_dict["priority"] = priority
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        dedup_key = d.pop("dedup_key")

        title = d.pop("title")

        description = d.pop("description", UNSET)

        priority = d.pop("priority", UNSET)

        def _parse_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))

        def _parse_intelligence_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        intelligence_class = _parse_intelligence_class(d.pop("intelligence_class", UNSET))

        ensure_task_request = cls(
            project_id=project_id,
            dedup_key=dedup_key,
            title=title,
            description=description,
            priority=priority,
            profile_id=profile_id,
            intelligence_class=intelligence_class,
        )

        ensure_task_request.additional_properties = d
        return ensure_task_request

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
