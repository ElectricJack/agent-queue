from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ask_decide_request_expected import AskDecideRequestExpected


T = TypeVar("T", bound="AskDecideRequest")


@_attrs_define
class AskDecideRequest:
    """
    Attributes:
        ask_id (str):
        expected (AskDecideRequestExpected):
        option_key (str):
        preview_id (str):
        idempotency_key (str):
        overall_note (None | str | Unset):
        dispositions (list[Any] | None | Unset):
        responder_class (None | str | Unset):
    """

    ask_id: str
    expected: AskDecideRequestExpected
    option_key: str
    preview_id: str
    idempotency_key: str
    overall_note: None | str | Unset = UNSET
    dispositions: list[Any] | None | Unset = UNSET
    responder_class: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        ask_id = self.ask_id

        expected = self.expected.to_dict()

        option_key = self.option_key

        preview_id = self.preview_id

        idempotency_key = self.idempotency_key

        overall_note: None | str | Unset
        if isinstance(self.overall_note, Unset):
            overall_note = UNSET
        else:
            overall_note = self.overall_note

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
                "ask_id": ask_id,
                "expected": expected,
                "option_key": option_key,
                "preview_id": preview_id,
                "idempotency_key": idempotency_key,
            }
        )
        if overall_note is not UNSET:
            field_dict["overall_note"] = overall_note
        if dispositions is not UNSET:
            field_dict["dispositions"] = dispositions
        if responder_class is not UNSET:
            field_dict["responder_class"] = responder_class

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_decide_request_expected import AskDecideRequestExpected

        d = dict(src_dict)
        ask_id = d.pop("ask_id")

        expected = AskDecideRequestExpected.from_dict(d.pop("expected"))

        option_key = d.pop("option_key")

        preview_id = d.pop("preview_id")

        idempotency_key = d.pop("idempotency_key")

        def _parse_overall_note(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        overall_note = _parse_overall_note(d.pop("overall_note", UNSET))

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

        ask_decide_request = cls(
            ask_id=ask_id,
            expected=expected,
            option_key=option_key,
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            overall_note=overall_note,
            dispositions=dispositions,
            responder_class=responder_class,
        )

        ask_decide_request.additional_properties = d
        return ask_decide_request

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
