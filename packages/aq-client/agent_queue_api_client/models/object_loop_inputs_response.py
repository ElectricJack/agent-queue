from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.object_loop_inputs_response_loops_item import ObjectLoopInputsResponseLoopsItem
    from ..models.object_loop_inputs_response_starts_item import ObjectLoopInputsResponseStartsItem


T = TypeVar("T", bound="ObjectLoopInputsResponse")


@_attrs_define
class ObjectLoopInputsResponse:
    """
    Attributes:
        starts (list[ObjectLoopInputsResponseStartsItem]):
        loops (list[ObjectLoopInputsResponseLoopsItem]):
        success (bool | Unset):  Default: True.
    """

    starts: list[ObjectLoopInputsResponseStartsItem]
    loops: list[ObjectLoopInputsResponseLoopsItem]
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        starts = []
        for starts_item_data in self.starts:
            starts_item = starts_item_data.to_dict()
            starts.append(starts_item)

        loops = []
        for loops_item_data in self.loops:
            loops_item = loops_item_data.to_dict()
            loops.append(loops_item)

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "starts": starts,
                "loops": loops,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.object_loop_inputs_response_loops_item import ObjectLoopInputsResponseLoopsItem
        from ..models.object_loop_inputs_response_starts_item import ObjectLoopInputsResponseStartsItem

        d = dict(src_dict)
        starts = []
        _starts = d.pop("starts")
        for starts_item_data in _starts:
            starts_item = ObjectLoopInputsResponseStartsItem.from_dict(starts_item_data)

            starts.append(starts_item)

        loops = []
        _loops = d.pop("loops")
        for loops_item_data in _loops:
            loops_item = ObjectLoopInputsResponseLoopsItem.from_dict(loops_item_data)

            loops.append(loops_item)

        success = d.pop("success", UNSET)

        object_loop_inputs_response = cls(
            starts=starts,
            loops=loops,
            success=success,
        )

        object_loop_inputs_response.additional_properties = d
        return object_loop_inputs_response

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
