from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.ask_cutover_destination import AskCutoverDestination


T = TypeVar("T", bound="AskCutoverStatusResult")


@_attrs_define
class AskCutoverStatusResult:
    """
    Attributes:
        project_id (str):
        configured_destination (str):
        destinations (list[AskCutoverDestination]):
    """

    project_id: str
    configured_destination: str
    destinations: list[AskCutoverDestination]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        configured_destination = self.configured_destination

        destinations = []
        for destinations_item_data in self.destinations:
            destinations_item = destinations_item_data.to_dict()
            destinations.append(destinations_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "configured_destination": configured_destination,
                "destinations": destinations,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_cutover_destination import AskCutoverDestination

        d = dict(src_dict)
        project_id = d.pop("project_id")

        configured_destination = d.pop("configured_destination")

        destinations = []
        _destinations = d.pop("destinations")
        for destinations_item_data in _destinations:
            destinations_item = AskCutoverDestination.from_dict(destinations_item_data)

            destinations.append(destinations_item)

        ask_cutover_status_result = cls(
            project_id=project_id,
            configured_destination=configured_destination,
            destinations=destinations,
        )

        ask_cutover_status_result.additional_properties = d
        return ask_cutover_status_result

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
