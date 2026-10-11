from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.ask_update_context_request_expected import AskUpdateContextRequestExpected


T = TypeVar("T", bound="AskUpdateContextRequest")


@_attrs_define
class AskUpdateContextRequest:
    """
    Attributes:
        ask_id (str):
        expected (AskUpdateContextRequestExpected):
    """

    ask_id: str
    expected: AskUpdateContextRequestExpected
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        ask_id = self.ask_id

        expected = self.expected.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "ask_id": ask_id,
                "expected": expected,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_update_context_request_expected import AskUpdateContextRequestExpected

        d = dict(src_dict)
        ask_id = d.pop("ask_id")

        expected = AskUpdateContextRequestExpected.from_dict(d.pop("expected"))

        ask_update_context_request = cls(
            ask_id=ask_id,
            expected=expected,
        )

        ask_update_context_request.additional_properties = d
        return ask_update_context_request

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
