from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ask_preview import AskPreview
    from ..models.ask_version import AskVersion


T = TypeVar("T", bound="AskPrepareResponse")


@_attrs_define
class AskPrepareResponse:
    """
    Attributes:
        preview (AskPreview):
        expected (AskVersion):
        success (bool | Unset):  Default: True.
    """

    preview: AskPreview
    expected: AskVersion
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        preview = self.preview.to_dict()

        expected = self.expected.to_dict()

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "preview": preview,
                "expected": expected,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_preview import AskPreview
        from ..models.ask_version import AskVersion

        d = dict(src_dict)
        preview = AskPreview.from_dict(d.pop("preview"))

        expected = AskVersion.from_dict(d.pop("expected"))

        success = d.pop("success", UNSET)

        ask_prepare_response = cls(
            preview=preview,
            expected=expected,
            success=success,
        )

        ask_prepare_response.additional_properties = d
        return ask_prepare_response

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
