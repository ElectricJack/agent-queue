from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.review_decide_request_expected_type_0 import ReviewDecideRequestExpectedType0


T = TypeVar("T", bound="ReviewDecideRequest")


@_attrs_define
class ReviewDecideRequest:
    """
    Attributes:
        review_id (str):
        revision (int):
        decision (str):
        note (None | str | Unset):
        preview_id (None | str | Unset):
        expected (None | ReviewDecideRequestExpectedType0 | Unset):
        idempotency_key (None | str | Unset):
        dispositions (list[Any] | None | Unset):
        responder_class (None | str | Unset): After request_changes or reject: intelligence-class hint for the new
            revision task, which the project's router routes. A responder profile is not accepted.
    """

    review_id: str
    revision: int
    decision: str
    note: None | str | Unset = UNSET
    preview_id: None | str | Unset = UNSET
    expected: None | ReviewDecideRequestExpectedType0 | Unset = UNSET
    idempotency_key: None | str | Unset = UNSET
    dispositions: list[Any] | None | Unset = UNSET
    responder_class: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.review_decide_request_expected_type_0 import ReviewDecideRequestExpectedType0

        review_id = self.review_id

        revision = self.revision

        decision = self.decision

        note: None | str | Unset
        if isinstance(self.note, Unset):
            note = UNSET
        else:
            note = self.note

        preview_id: None | str | Unset
        if isinstance(self.preview_id, Unset):
            preview_id = UNSET
        else:
            preview_id = self.preview_id

        expected: dict[str, Any] | None | Unset
        if isinstance(self.expected, Unset):
            expected = UNSET
        elif isinstance(self.expected, ReviewDecideRequestExpectedType0):
            expected = self.expected.to_dict()
        else:
            expected = self.expected

        idempotency_key: None | str | Unset
        if isinstance(self.idempotency_key, Unset):
            idempotency_key = UNSET
        else:
            idempotency_key = self.idempotency_key

        dispositions: list[Any] | None | Unset
        if isinstance(self.dispositions, Unset):
            dispositions = UNSET
        elif isinstance(self.dispositions, list):
            dispositions = self.dispositions

        else:
            dispositions = self.dispositions

        responder_class: None | str | Unset
        if isinstance(self.responder_class, Unset):
            responder_class = UNSET
        else:
            responder_class = self.responder_class

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
        if preview_id is not UNSET:
            field_dict["preview_id"] = preview_id
        if expected is not UNSET:
            field_dict["expected"] = expected
        if idempotency_key is not UNSET:
            field_dict["idempotency_key"] = idempotency_key
        if dispositions is not UNSET:
            field_dict["dispositions"] = dispositions
        if responder_class is not UNSET:
            field_dict["responder_class"] = responder_class

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.review_decide_request_expected_type_0 import ReviewDecideRequestExpectedType0

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

        def _parse_preview_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        preview_id = _parse_preview_id(d.pop("preview_id", UNSET))

        def _parse_expected(data: object) -> None | ReviewDecideRequestExpectedType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                expected_type_0 = ReviewDecideRequestExpectedType0.from_dict(data)

                return expected_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ReviewDecideRequestExpectedType0 | Unset, data)

        expected = _parse_expected(d.pop("expected", UNSET))

        def _parse_idempotency_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        idempotency_key = _parse_idempotency_key(d.pop("idempotency_key", UNSET))

        def _parse_dispositions(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                dispositions_type_0 = cast(list[Any], data)

                return dispositions_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        dispositions = _parse_dispositions(d.pop("dispositions", UNSET))

        def _parse_responder_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        responder_class = _parse_responder_class(d.pop("responder_class", UNSET))

        review_decide_request = cls(
            review_id=review_id,
            revision=revision,
            decision=decision,
            note=note,
            preview_id=preview_id,
            expected=expected,
            idempotency_key=idempotency_key,
            dispositions=dispositions,
            responder_class=responder_class,
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
