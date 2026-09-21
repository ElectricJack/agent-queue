from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReviewSubmitResponse")


@_attrs_define
class ReviewSubmitResponse:
    """
    Attributes:
        review_id (str):
        revision (int):
        vault_path (str):
        success (bool | Unset):  Default: True.
    """

    review_id: str
    revision: int
    vault_path: str
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        review_id = self.review_id

        revision = self.revision

        vault_path = self.vault_path

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "review_id": review_id,
                "revision": revision,
                "vault_path": vault_path,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        review_id = d.pop("review_id")

        revision = d.pop("revision")

        vault_path = d.pop("vault_path")

        success = d.pop("success", UNSET)

        review_submit_response = cls(
            review_id=review_id,
            revision=revision,
            vault_path=vault_path,
            success=success,
        )

        review_submit_response.additional_properties = d
        return review_submit_response

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
