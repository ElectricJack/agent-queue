from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SessionMessageRequest")


@_attrs_define
class SessionMessageRequest:
    """Body of ``POST /api/sessions/{name}/message`` (design §7).

    Attributes:
        body (str): Markdown message body
        from_ (str | Unset): Sender id, e.g. 'discord:1234' or 'cli' Default: 'user'.
        from_kind (str | Unset): Sender kind: session | user | system Default: 'user'.
        thread_id (None | str | Unset): Conversation grouping key
        subject (None | str | Unset): Optional subject line
        priority (int | Unset): Delivery ordering, lower first Default: 100.
    """

    body: str
    from_: str | Unset = "user"
    from_kind: str | Unset = "user"
    thread_id: None | str | Unset = UNSET
    subject: None | str | Unset = UNSET
    priority: int | Unset = 100
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        body = self.body

        from_ = self.from_

        from_kind = self.from_kind

        thread_id: None | str | Unset
        if isinstance(self.thread_id, Unset):
            thread_id = UNSET
        else:
            thread_id = self.thread_id

        subject: None | str | Unset
        if isinstance(self.subject, Unset):
            subject = UNSET
        else:
            subject = self.subject

        priority = self.priority

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "body": body,
            }
        )
        if from_ is not UNSET:
            field_dict["from"] = from_
        if from_kind is not UNSET:
            field_dict["from_kind"] = from_kind
        if thread_id is not UNSET:
            field_dict["thread_id"] = thread_id
        if subject is not UNSET:
            field_dict["subject"] = subject
        if priority is not UNSET:
            field_dict["priority"] = priority

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        body = d.pop("body")

        from_ = d.pop("from", UNSET)

        from_kind = d.pop("from_kind", UNSET)

        def _parse_thread_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        thread_id = _parse_thread_id(d.pop("thread_id", UNSET))

        def _parse_subject(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        subject = _parse_subject(d.pop("subject", UNSET))

        priority = d.pop("priority", UNSET)

        session_message_request = cls(
            body=body,
            from_=from_,
            from_kind=from_kind,
            thread_id=thread_id,
            subject=subject,
            priority=priority,
        )

        session_message_request.additional_properties = d
        return session_message_request

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
