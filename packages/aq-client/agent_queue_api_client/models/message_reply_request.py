from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="MessageReplyRequest")


@_attrs_define
class MessageReplyRequest:
    """
    Attributes:
        message_id (str): Message being replied to
        body (str): Markdown reply body
        client_key (None | str | Unset):
        claim_epoch (int | None | Unset):
        subject (None | str | Unset): Optional subject line
        from_kind (None | str | Unset): Override the inferred replier kind
        from_id (None | str | Unset): Override the inferred replier id
        via (None | str | Unset): Delivery marker, e.g. transcript_tail
    """

    message_id: str
    body: str
    client_key: None | str | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET
    subject: None | str | Unset = UNSET
    from_kind: None | str | Unset = UNSET
    from_id: None | str | Unset = UNSET
    via: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        message_id = self.message_id

        body = self.body

        client_key: None | str | Unset
        if isinstance(self.client_key, Unset):
            client_key = UNSET
        else:
            client_key = self.client_key

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        subject: None | str | Unset
        if isinstance(self.subject, Unset):
            subject = UNSET
        else:
            subject = self.subject

        from_kind: None | str | Unset
        if isinstance(self.from_kind, Unset):
            from_kind = UNSET
        else:
            from_kind = self.from_kind

        from_id: None | str | Unset
        if isinstance(self.from_id, Unset):
            from_id = UNSET
        else:
            from_id = self.from_id

        via: None | str | Unset
        if isinstance(self.via, Unset):
            via = UNSET
        else:
            via = self.via

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "message_id": message_id,
                "body": body,
            }
        )
        if client_key is not UNSET:
            field_dict["client_key"] = client_key
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch
        if subject is not UNSET:
            field_dict["subject"] = subject
        if from_kind is not UNSET:
            field_dict["from_kind"] = from_kind
        if from_id is not UNSET:
            field_dict["from_id"] = from_id
        if via is not UNSET:
            field_dict["via"] = via

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        message_id = d.pop("message_id")

        body = d.pop("body")

        def _parse_client_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        client_key = _parse_client_key(d.pop("client_key", UNSET))

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        def _parse_subject(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        subject = _parse_subject(d.pop("subject", UNSET))

        def _parse_from_kind(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        from_kind = _parse_from_kind(d.pop("from_kind", UNSET))

        def _parse_from_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        from_id = _parse_from_id(d.pop("from_id", UNSET))

        def _parse_via(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        via = _parse_via(d.pop("via", UNSET))

        message_reply_request = cls(
            message_id=message_id,
            body=body,
            client_key=client_key,
            claim_epoch=claim_epoch,
            subject=subject,
            from_kind=from_kind,
            from_id=from_id,
            via=via,
        )

        message_reply_request.additional_properties = d
        return message_reply_request

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
