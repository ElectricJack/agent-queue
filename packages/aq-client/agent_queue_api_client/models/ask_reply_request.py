from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ask_reply_request_expected import AskReplyRequestExpected


T = TypeVar("T", bound="AskReplyRequest")


@_attrs_define
class AskReplyRequest:
    """
    Attributes:
        ask_id (str):
        text (str):
        idempotency_key (str):
        expected (AskReplyRequestExpected):
        intent (str | Unset):  Default: 'discuss'.
        post_decision (bool | Unset):  Default: False.
    """

    ask_id: str
    text: str
    idempotency_key: str
    expected: AskReplyRequestExpected
    intent: str | Unset = "discuss"
    post_decision: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        ask_id = self.ask_id

        text = self.text

        idempotency_key = self.idempotency_key

        expected = self.expected.to_dict()

        intent = self.intent

        post_decision = self.post_decision

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "ask_id": ask_id,
                "text": text,
                "idempotency_key": idempotency_key,
                "expected": expected,
            }
        )
        if intent is not UNSET:
            field_dict["intent"] = intent
        if post_decision is not UNSET:
            field_dict["post_decision"] = post_decision

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_reply_request_expected import AskReplyRequestExpected

        d = dict(src_dict)
        ask_id = d.pop("ask_id")

        text = d.pop("text")

        idempotency_key = d.pop("idempotency_key")

        expected = AskReplyRequestExpected.from_dict(d.pop("expected"))

        intent = d.pop("intent", UNSET)

        post_decision = d.pop("post_decision", UNSET)

        ask_reply_request = cls(
            ask_id=ask_id,
            text=text,
            idempotency_key=idempotency_key,
            expected=expected,
            intent=intent,
            post_decision=post_decision,
        )

        ask_reply_request.additional_properties = d
        return ask_reply_request

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
