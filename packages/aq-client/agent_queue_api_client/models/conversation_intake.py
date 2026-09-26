from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.conversation_intake_ignored import ConversationIntakeIgnored


T = TypeVar("T", bound="ConversationIntake")


@_attrs_define
class ConversationIntake:
    """
    Attributes:
        available (bool):
        window_seconds (int):
        total (int):
        ignored (ConversationIntakeIgnored):
    """

    available: bool
    window_seconds: int
    total: int
    ignored: ConversationIntakeIgnored
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        available = self.available

        window_seconds = self.window_seconds

        total = self.total

        ignored = self.ignored.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "available": available,
                "window_seconds": window_seconds,
                "total": total,
                "ignored": ignored,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.conversation_intake_ignored import ConversationIntakeIgnored

        d = dict(src_dict)
        available = d.pop("available")

        window_seconds = d.pop("window_seconds")

        total = d.pop("total")

        ignored = ConversationIntakeIgnored.from_dict(d.pop("ignored"))

        conversation_intake = cls(
            available=available,
            window_seconds=window_seconds,
            total=total,
            ignored=ignored,
        )

        conversation_intake.additional_properties = d
        return conversation_intake

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
