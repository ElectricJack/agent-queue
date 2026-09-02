from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ready_task import ReadyTask
    from ..models.withheld_task import WithheldTask


T = TypeVar("T", bound="ProjectReadyResponse")


@_attrs_define
class ProjectReadyResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        ready (list[ReadyTask] | Unset):
        withheld (list[WithheldTask] | Unset):
    """

    success: bool | Unset = True
    ready: list[ReadyTask] | Unset = UNSET
    withheld: list[WithheldTask] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        ready: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.ready, Unset):
            ready = []
            for ready_item_data in self.ready:
                ready_item = ready_item_data.to_dict()
                ready.append(ready_item)

        withheld: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.withheld, Unset):
            withheld = []
            for withheld_item_data in self.withheld:
                withheld_item = withheld_item_data.to_dict()
                withheld.append(withheld_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if ready is not UNSET:
            field_dict["ready"] = ready
        if withheld is not UNSET:
            field_dict["withheld"] = withheld

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ready_task import ReadyTask
        from ..models.withheld_task import WithheldTask

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _ready = d.pop("ready", UNSET)
        ready: list[ReadyTask] | Unset = UNSET
        if _ready is not UNSET:
            ready = []
            for ready_item_data in _ready:
                ready_item = ReadyTask.from_dict(ready_item_data)

                ready.append(ready_item)

        _withheld = d.pop("withheld", UNSET)
        withheld: list[WithheldTask] | Unset = UNSET
        if _withheld is not UNSET:
            withheld = []
            for withheld_item_data in _withheld:
                withheld_item = WithheldTask.from_dict(withheld_item_data)

                withheld.append(withheld_item)

        project_ready_response = cls(
            success=success,
            ready=ready,
            withheld=withheld,
        )

        project_ready_response.additional_properties = d
        return project_ready_response

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
