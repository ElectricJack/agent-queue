from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="CollaborationMessageRecord")


@_attrs_define
class CollaborationMessageRecord:
    """One accepted send; ``body`` is null once content retention has elapsed.

    Attributes:
        seq (int):
        sender_task_id (str):
        created_at (float):
        message_id (None | str | Unset):
        subject (None | str | Unset):
        body (None | str | Unset):
    """

    seq: int
    sender_task_id: str
    created_at: float
    message_id: None | str | Unset = UNSET
    subject: None | str | Unset = UNSET
    body: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        seq = self.seq

        sender_task_id = self.sender_task_id

        created_at = self.created_at

        message_id: None | str | Unset
        if isinstance(self.message_id, Unset):
            message_id = UNSET
        else:
            message_id = self.message_id

        subject: None | str | Unset
        if isinstance(self.subject, Unset):
            subject = UNSET
        else:
            subject = self.subject

        body: None | str | Unset
        if isinstance(self.body, Unset):
            body = UNSET
        else:
            body = self.body

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "seq": seq,
                "sender_task_id": sender_task_id,
                "created_at": created_at,
            }
        )
        if message_id is not UNSET:
            field_dict["message_id"] = message_id
        if subject is not UNSET:
            field_dict["subject"] = subject
        if body is not UNSET:
            field_dict["body"] = body

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        seq = d.pop("seq")

        sender_task_id = d.pop("sender_task_id")

        created_at = d.pop("created_at")

        def _parse_message_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        message_id = _parse_message_id(d.pop("message_id", UNSET))

        def _parse_subject(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        subject = _parse_subject(d.pop("subject", UNSET))

        def _parse_body(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        body = _parse_body(d.pop("body", UNSET))

        collaboration_message_record = cls(
            seq=seq,
            sender_task_id=sender_task_id,
            created_at=created_at,
            message_id=message_id,
            subject=subject,
            body=body,
        )

        return collaboration_message_record
