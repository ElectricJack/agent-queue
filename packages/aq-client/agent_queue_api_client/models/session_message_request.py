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
        from_ (None | str | Unset): Authorized sender id; omit for server default
        from_kind (None | str | Unset): Authorized sender kind: session | user | system; omit for server default
        thread_id (None | str | Unset): Conversation grouping key
        subject (None | str | Unset): Optional subject line
        priority (int | Unset): Delivery ordering, lower first Default: 100.
        claim_epoch (int | None | Unset):
    """

    body: str
    from_: None | str | Unset = UNSET
    from_kind: None | str | Unset = UNSET
    thread_id: None | str | Unset = UNSET
    subject: None | str | Unset = UNSET
    priority: int | Unset = 100
    claim_epoch: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        body = self.body

        from_: None | str | Unset
        if isinstance(self.from_, Unset):
            from_ = UNSET
        else:
            from_ = self.from_

        from_kind: None | str | Unset
        if isinstance(self.from_kind, Unset):
            from_kind = UNSET
        else:
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

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

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
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        body = d.pop("body")

        def _parse_from_(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        from_ = _parse_from_(d.pop("from", UNSET))

        def _parse_from_kind(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        from_kind = _parse_from_kind(d.pop("from_kind", UNSET))

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

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        session_message_request = cls(
            body=body,
            from_=from_,
            from_kind=from_kind,
            thread_id=thread_id,
            subject=subject,
            priority=priority,
            claim_epoch=claim_epoch,
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
