from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.diff_item import DiffItem
    from ..models.placeholder import Placeholder
    from ..models.policy_diff_response_values import PolicyDiffResponseValues


T = TypeVar("T", bound="PolicyDiffResponse")


@_attrs_define
class PolicyDiffResponse:
    """
    Attributes:
        name (str):
        project_id (str):
        items (list[DiffItem]):
        placeholders (list[Placeholder]):
        values (PolicyDiffResponseValues):
        success (bool | Unset):  Default: True.
    """

    name: str
    project_id: str
    items: list[DiffItem]
    placeholders: list[Placeholder]
    values: PolicyDiffResponseValues
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        project_id = self.project_id

        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        placeholders = []
        for placeholders_item_data in self.placeholders:
            placeholders_item = placeholders_item_data.to_dict()
            placeholders.append(placeholders_item)

        values = self.values.to_dict()

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
                "project_id": project_id,
                "items": items,
                "placeholders": placeholders,
                "values": values,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.diff_item import DiffItem
        from ..models.placeholder import Placeholder
        from ..models.policy_diff_response_values import PolicyDiffResponseValues

        d = dict(src_dict)
        name = d.pop("name")

        project_id = d.pop("project_id")

        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = DiffItem.from_dict(items_item_data)

            items.append(items_item)

        placeholders = []
        _placeholders = d.pop("placeholders")
        for placeholders_item_data in _placeholders:
            placeholders_item = Placeholder.from_dict(placeholders_item_data)

            placeholders.append(placeholders_item)

        values = PolicyDiffResponseValues.from_dict(d.pop("values"))

        success = d.pop("success", UNSET)

        policy_diff_response = cls(
            name=name,
            project_id=project_id,
            items=items,
            placeholders=placeholders,
            values=values,
            success=success,
        )

        policy_diff_response.additional_properties = d
        return policy_diff_response

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
