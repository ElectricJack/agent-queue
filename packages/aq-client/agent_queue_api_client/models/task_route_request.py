from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskRouteRequest")


@_attrs_define
class TaskRouteRequest:
    """
    Attributes:
        task_id (str): Task ID to re-route
        intelligence_class (None | str | Unset): New intelligence-class hint for the router (e.g. 'fast-high',
            'standard-high', 'deep-high'); the router honours it within its policy's bounds for the kind. Omit to keep the
            task's hint; an empty value clears it.
        task_type (None | str | Unset): New kind for the router (e.g. 'feature', 'bugfix', 'design', 'docs'). Omit to
            keep the task's kind; an empty value clears it.
        prefer (None | str | Unset): Routing preference for the router to weigh before scoring: a harness id (e.g.
            'codex') or a worker profile id. The router still picks the route; this only decides whether it may route
            elsewhere. Omit to keep the task's preference; an empty value clears it. An unknown name, a disabled profile and
            a non-worker profile are refused.
        prefer_mode (str | Unset): How the router may refuse 'prefer': 'soft' (default) routes to it when it has
            headroom and routes normally when it does not; 'strict' admits only that harness or profile and holds the task
            rather than falling back to another one. On 'aq task route' a mode with no 'prefer' keeps the stored target.
            Default: 'soft'.
        reason (None | str | Unset): Why the task is re-routed; posted as a task comment.
    """

    task_id: str
    intelligence_class: None | str | Unset = UNSET
    task_type: None | str | Unset = UNSET
    prefer: None | str | Unset = UNSET
    prefer_mode: str | Unset = "soft"
    reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        intelligence_class: None | str | Unset
        if isinstance(self.intelligence_class, Unset):
            intelligence_class = UNSET
        else:
            intelligence_class = self.intelligence_class

        task_type: None | str | Unset
        if isinstance(self.task_type, Unset):
            task_type = UNSET
        else:
            task_type = self.task_type

        prefer: None | str | Unset
        if isinstance(self.prefer, Unset):
            prefer = UNSET
        else:
            prefer = self.prefer

        prefer_mode = self.prefer_mode

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
            }
        )
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if task_type is not UNSET:
            field_dict["task_type"] = task_type
        if prefer is not UNSET:
            field_dict["prefer"] = prefer
        if prefer_mode is not UNSET:
            field_dict["prefer_mode"] = prefer_mode
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        def _parse_intelligence_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        intelligence_class = _parse_intelligence_class(d.pop("intelligence_class", UNSET))

        def _parse_task_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_type = _parse_task_type(d.pop("task_type", UNSET))

        def _parse_prefer(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        prefer = _parse_prefer(d.pop("prefer", UNSET))

        prefer_mode = d.pop("prefer_mode", UNSET)

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        task_route_request = cls(
            task_id=task_id,
            intelligence_class=intelligence_class,
            task_type=task_type,
            prefer=prefer,
            prefer_mode=prefer_mode,
            reason=reason,
        )

        task_route_request.additional_properties = d
        return task_route_request

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
