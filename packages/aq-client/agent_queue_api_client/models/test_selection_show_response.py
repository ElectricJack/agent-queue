from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.test_selection_show_response_observations_item import TestSelectionShowResponseObservationsItem
    from ..models.test_selection_show_response_selection import TestSelectionShowResponseSelection


T = TypeVar("T", bound="TestSelectionShowResponse")


@_attrs_define
class TestSelectionShowResponse:
    """
    Attributes:
        selection (TestSelectionShowResponseSelection):
        observations (list[TestSelectionShowResponseObservationsItem]):
        success (bool | Unset):  Default: True.
    """

    selection: TestSelectionShowResponseSelection
    observations: list[TestSelectionShowResponseObservationsItem]
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        selection = self.selection.to_dict()

        observations = []
        for observations_item_data in self.observations:
            observations_item = observations_item_data.to_dict()
            observations.append(observations_item)

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "selection": selection,
                "observations": observations,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.test_selection_show_response_observations_item import TestSelectionShowResponseObservationsItem
        from ..models.test_selection_show_response_selection import TestSelectionShowResponseSelection

        d = dict(src_dict)
        selection = TestSelectionShowResponseSelection.from_dict(d.pop("selection"))

        observations = []
        _observations = d.pop("observations")
        for observations_item_data in _observations:
            observations_item = TestSelectionShowResponseObservationsItem.from_dict(observations_item_data)

            observations.append(observations_item)

        success = d.pop("success", UNSET)

        test_selection_show_response = cls(
            selection=selection,
            observations=observations,
            success=success,
        )

        test_selection_show_response.additional_properties = d
        return test_selection_show_response

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
