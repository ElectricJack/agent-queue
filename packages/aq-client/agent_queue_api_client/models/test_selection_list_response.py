from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.test_selection_list_response_selections_item import TestSelectionListResponseSelectionsItem


T = TypeVar("T", bound="TestSelectionListResponse")


@_attrs_define
class TestSelectionListResponse:
    """
    Attributes:
        selections (list[TestSelectionListResponseSelectionsItem]):
        success (bool | Unset):  Default: True.
    """

    selections: list[TestSelectionListResponseSelectionsItem]
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        selections = []
        for selections_item_data in self.selections:
            selections_item = selections_item_data.to_dict()
            selections.append(selections_item)

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "selections": selections,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.test_selection_list_response_selections_item import TestSelectionListResponseSelectionsItem

        d = dict(src_dict)
        selections = []
        _selections = d.pop("selections")
        for selections_item_data in _selections:
            selections_item = TestSelectionListResponseSelectionsItem.from_dict(selections_item_data)

            selections.append(selections_item)

        success = d.pop("success", UNSET)

        test_selection_list_response = cls(
            selections=selections,
            success=success,
        )

        test_selection_list_response.additional_properties = d
        return test_selection_list_response

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
