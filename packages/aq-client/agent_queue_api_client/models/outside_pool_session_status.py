from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="OutsidePoolSessionStatus")


@_attrs_define
class OutsidePoolSessionStatus:
    """A live task-lifecycle session shadowing this pool's execution route.

    Attributes:
        session_id (str):
        profile_id (str):
        harness (str):
        intelligence_class (str):
        name (str):
        state (str):
        started_at (float):
        project_id (None | str | Unset):
        task_id (None | str | Unset):
        task_title (None | str | Unset):
    """

    session_id: str
    profile_id: str
    harness: str
    intelligence_class: str
    name: str
    state: str
    started_at: float
    project_id: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    task_title: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        session_id = self.session_id

        profile_id = self.profile_id

        harness = self.harness

        intelligence_class = self.intelligence_class

        name = self.name

        state = self.state

        started_at = self.started_at

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

        task_title: None | str | Unset
        if isinstance(self.task_title, Unset):
            task_title = UNSET
        else:
            task_title = self.task_title

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "session_id": session_id,
                "profile_id": profile_id,
                "harness": harness,
                "intelligence_class": intelligence_class,
                "name": name,
                "state": state,
                "started_at": started_at,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if task_title is not UNSET:
            field_dict["task_title"] = task_title

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        session_id = d.pop("session_id")

        profile_id = d.pop("profile_id")

        harness = d.pop("harness")

        intelligence_class = d.pop("intelligence_class")

        name = d.pop("name")

        state = d.pop("state")

        started_at = d.pop("started_at")

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

        def _parse_task_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_title = _parse_task_title(d.pop("task_title", UNSET))

        outside_pool_session_status = cls(
            session_id=session_id,
            profile_id=profile_id,
            harness=harness,
            intelligence_class=intelligence_class,
            name=name,
            state=state,
            started_at=started_at,
            project_id=project_id,
            task_id=task_id,
            task_title=task_title,
        )

        outside_pool_session_status.additional_properties = d
        return outside_pool_session_status

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
