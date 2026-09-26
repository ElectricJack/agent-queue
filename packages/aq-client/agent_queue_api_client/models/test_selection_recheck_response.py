from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TestSelectionRecheckResponse")


@_attrs_define
class TestSelectionRecheckResponse:
    """
    Attributes:
        stale (bool):
        fingerprint (str):
        recorded_fingerprint (str):
        success (bool | Unset):  Default: True.
    """

    stale: bool
    fingerprint: str
    recorded_fingerprint: str
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        stale = self.stale

        fingerprint = self.fingerprint

        recorded_fingerprint = self.recorded_fingerprint

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "stale": stale,
                "fingerprint": fingerprint,
                "recorded_fingerprint": recorded_fingerprint,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        stale = d.pop("stale")

        fingerprint = d.pop("fingerprint")

        recorded_fingerprint = d.pop("recorded_fingerprint")

        success = d.pop("success", UNSET)

        test_selection_recheck_response = cls(
            stale=stale,
            fingerprint=fingerprint,
            recorded_fingerprint=recorded_fingerprint,
            success=success,
        )

        test_selection_recheck_response.additional_properties = d
        return test_selection_recheck_response

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
