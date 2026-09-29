from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_route_cleared_route import TaskRouteClearedRoute


T = TypeVar("T", bound="TaskRouteResponse")


@_attrs_define
class TaskRouteResponse:
    """``task_route`` re-runs the router: the task is ``unrouted`` again, with its hints.

    Attributes:
        task_id (str):
        success (bool | Unset):  Default: True.
        route_source (str | Unset):  Default: 'unrouted'.
        class_hint (None | str | Unset):
        task_type (None | str | Unset):
        cleared (None | TaskRouteClearedRoute | Unset):
    """

    task_id: str
    success: bool | Unset = True
    route_source: str | Unset = "unrouted"
    class_hint: None | str | Unset = UNSET
    task_type: None | str | Unset = UNSET
    cleared: None | TaskRouteClearedRoute | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.task_route_cleared_route import TaskRouteClearedRoute

        task_id = self.task_id

        success = self.success

        route_source = self.route_source

        class_hint: None | str | Unset
        if isinstance(self.class_hint, Unset):
            class_hint = UNSET
        else:
            class_hint = self.class_hint

        task_type: None | str | Unset
        if isinstance(self.task_type, Unset):
            task_type = UNSET
        else:
            task_type = self.task_type

        cleared: dict[str, Any] | None | Unset
        if isinstance(self.cleared, Unset):
            cleared = UNSET
        elif isinstance(self.cleared, TaskRouteClearedRoute):
            cleared = self.cleared.to_dict()
        else:
            cleared = self.cleared

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if route_source is not UNSET:
            field_dict["route_source"] = route_source
        if class_hint is not UNSET:
            field_dict["class_hint"] = class_hint
        if task_type is not UNSET:
            field_dict["task_type"] = task_type
        if cleared is not UNSET:
            field_dict["cleared"] = cleared

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_route_cleared_route import TaskRouteClearedRoute

        d = dict(src_dict)
        task_id = d.pop("task_id")

        success = d.pop("success", UNSET)

        route_source = d.pop("route_source", UNSET)

        def _parse_class_hint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        class_hint = _parse_class_hint(d.pop("class_hint", UNSET))

        def _parse_task_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_type = _parse_task_type(d.pop("task_type", UNSET))

        def _parse_cleared(data: object) -> None | TaskRouteClearedRoute | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                cleared_type_0 = TaskRouteClearedRoute.from_dict(data)

                return cleared_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskRouteClearedRoute | Unset, data)

        cleared = _parse_cleared(d.pop("cleared", UNSET))

        task_route_response = cls(
            task_id=task_id,
            success=success,
            route_source=route_source,
            class_hint=class_hint,
            task_type=task_type,
            cleared=cleared,
        )

        task_route_response.additional_properties = d
        return task_route_response

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
