from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="AskCutoverRequest")


@_attrs_define
class AskCutoverRequest:
    """
    Attributes:
        project_id (str):
        mode (str):
        expected_generation (int):
    """

    project_id: str
    mode: str
    expected_generation: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        mode = self.mode

        expected_generation = self.expected_generation

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "mode": mode,
                "expected_generation": expected_generation,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        mode = d.pop("mode")

        expected_generation = d.pop("expected_generation")

        ask_cutover_request = cls(
            project_id=project_id,
            mode=mode,
            expected_generation=expected_generation,
        )

        ask_cutover_request.additional_properties = d
        return ask_cutover_request

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
