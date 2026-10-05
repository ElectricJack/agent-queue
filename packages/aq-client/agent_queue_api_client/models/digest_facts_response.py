from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.digest_facts_response_facts import DigestFactsResponseFacts


T = TypeVar("T", bound="DigestFactsResponse")


@_attrs_define
class DigestFactsResponse:
    """One window's frozen evidence, as the author reads it.

    Attributes:
        request_id (str):
        window_id (str):
        state (str):
        deadline (float):
        facts (DigestFactsResponseFacts):
        facts_hash (str):
        success (bool | Unset):  Default: True.
        seconds_remaining (float | Unset):  Default: 0.0.
    """

    request_id: str
    window_id: str
    state: str
    deadline: float
    facts: DigestFactsResponseFacts
    facts_hash: str
    success: bool | Unset = True
    seconds_remaining: float | Unset = 0.0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        request_id = self.request_id

        window_id = self.window_id

        state = self.state

        deadline = self.deadline

        facts = self.facts.to_dict()

        facts_hash = self.facts_hash

        success = self.success

        seconds_remaining = self.seconds_remaining

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "request_id": request_id,
                "window_id": window_id,
                "state": state,
                "deadline": deadline,
                "facts": facts,
                "facts_hash": facts_hash,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if seconds_remaining is not UNSET:
            field_dict["seconds_remaining"] = seconds_remaining

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.digest_facts_response_facts import DigestFactsResponseFacts

        d = dict(src_dict)
        request_id = d.pop("request_id")

        window_id = d.pop("window_id")

        state = d.pop("state")

        deadline = d.pop("deadline")

        facts = DigestFactsResponseFacts.from_dict(d.pop("facts"))

        facts_hash = d.pop("facts_hash")

        success = d.pop("success", UNSET)

        seconds_remaining = d.pop("seconds_remaining", UNSET)

        digest_facts_response = cls(
            request_id=request_id,
            window_id=window_id,
            state=state,
            deadline=deadline,
            facts=facts,
            facts_hash=facts_hash,
            success=success,
            seconds_remaining=seconds_remaining,
        )

        digest_facts_response.additional_properties = d
        return digest_facts_response

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
