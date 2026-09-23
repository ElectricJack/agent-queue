from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReviewDecideRequest")


@_attrs_define
class ReviewDecideRequest:
    """
    Attributes:
        review_id (str):
        revision (int):
        decision (str):
        note (None | str | Unset):
        responder_class (None | str | Unset): Who revises after feedback: intelligence class for the new revision task
            (request_changes only).
        responder_profile (None | str | Unset): Optional worker profile for that revision class (request_changes only).
    """

    review_id: str
    revision: int
    decision: str
    note: None | str | Unset = UNSET
    responder_class: None | str | Unset = UNSET
    responder_profile: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        review_id = self.review_id

        revision = self.revision

        decision = self.decision

        note: None | str | Unset
        if isinstance(self.note, Unset):
            note = UNSET
        else:
            note = self.note

        responder_class: None | str | Unset
        if isinstance(self.responder_class, Unset):
            responder_class = UNSET
        else:
            responder_class = self.responder_class

        responder_profile: None | str | Unset
        if isinstance(self.responder_profile, Unset):
            responder_profile = UNSET
        else:
            responder_profile = self.responder_profile

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "review_id": review_id,
                "revision": revision,
                "decision": decision,
            }
        )
        if note is not UNSET:
            field_dict["note"] = note
        if responder_class is not UNSET:
            field_dict["responder_class"] = responder_class
        if responder_profile is not UNSET:
            field_dict["responder_profile"] = responder_profile

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        review_id = d.pop("review_id")

        revision = d.pop("revision")

        decision = d.pop("decision")

        def _parse_note(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        note = _parse_note(d.pop("note", UNSET))

        def _parse_responder_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        responder_class = _parse_responder_class(d.pop("responder_class", UNSET))

        def _parse_responder_profile(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        responder_profile = _parse_responder_profile(d.pop("responder_profile", UNSET))

        review_decide_request = cls(
            review_id=review_id,
            revision=revision,
            decision=decision,
            note=note,
            responder_class=responder_class,
            responder_profile=responder_profile,
        )

        review_decide_request.additional_properties = d
        return review_decide_request

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
