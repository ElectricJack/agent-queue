from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="ConversationLimits")


@_attrs_define
class ConversationLimits:
    """
    Attributes:
        max_input_chars (int):
        author_window_limit (int):
        channel_window_limit (int):
        window_seconds (int):
        max_reply_chars (int):
    """

    max_input_chars: int
    author_window_limit: int
    channel_window_limit: int
    window_seconds: int
    max_reply_chars: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        max_input_chars = self.max_input_chars

        author_window_limit = self.author_window_limit

        channel_window_limit = self.channel_window_limit

        window_seconds = self.window_seconds

        max_reply_chars = self.max_reply_chars

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "max_input_chars": max_input_chars,
                "author_window_limit": author_window_limit,
                "channel_window_limit": channel_window_limit,
                "window_seconds": window_seconds,
                "max_reply_chars": max_reply_chars,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        max_input_chars = d.pop("max_input_chars")

        author_window_limit = d.pop("author_window_limit")

        channel_window_limit = d.pop("channel_window_limit")

        window_seconds = d.pop("window_seconds")

        max_reply_chars = d.pop("max_reply_chars")

        conversation_limits = cls(
            max_input_chars=max_input_chars,
            author_window_limit=author_window_limit,
            channel_window_limit=channel_window_limit,
            window_seconds=window_seconds,
            max_reply_chars=max_reply_chars,
        )

        conversation_limits.additional_properties = d
        return conversation_limits

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
