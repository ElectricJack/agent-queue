from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskRouteOverrideRequest")


@_attrs_define
class TaskRouteOverrideRequest:
    """
    Attributes:
        task_id (str): Queued task ID to override
        profile_id (str): Worker profile that must run the task
        reason (str): Why the router's choice is overridden (10-400 characters); recorded on the route, the event and a
            task comment
        intelligence_class (None | str | Unset): Class to run at. Default: the profile's fixed class, else the task's
            class hint. Must be one the profile can run.
    """

    task_id: str
    profile_id: str
    reason: str
    intelligence_class: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        profile_id = self.profile_id

        reason = self.reason

        intelligence_class: None | str | Unset
        if isinstance(self.intelligence_class, Unset):
            intelligence_class = UNSET
        else:
            intelligence_class = self.intelligence_class

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "profile_id": profile_id,
                "reason": reason,
            }
        )
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        profile_id = d.pop("profile_id")

        reason = d.pop("reason")

        def _parse_intelligence_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        intelligence_class = _parse_intelligence_class(d.pop("intelligence_class", UNSET))

        task_route_override_request = cls(
            task_id=task_id,
            profile_id=profile_id,
            reason=reason,
            intelligence_class=intelligence_class,
        )

        task_route_override_request.additional_properties = d
        return task_route_override_request

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
