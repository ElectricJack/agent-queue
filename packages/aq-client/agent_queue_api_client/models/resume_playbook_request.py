from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.resume_playbook_request_expected_type_0 import ResumePlaybookRequestExpectedType0


T = TypeVar("T", bound="ResumePlaybookRequest")


@_attrs_define
class ResumePlaybookRequest:
    """
    Attributes:
        run_id (str): The playbook run ID to resume
        human_input (str): Your review decision or feedback text. This is added to the conversation history and used to
            determine the next transition.
        expected (None | ResumePlaybookRequestExpectedType0 | Unset):
        preview_id (None | str | Unset):
        idempotency_key (None | str | Unset):
        overall_note (None | str | Unset):
    """

    run_id: str
    human_input: str
    expected: None | ResumePlaybookRequestExpectedType0 | Unset = UNSET
    preview_id: None | str | Unset = UNSET
    idempotency_key: None | str | Unset = UNSET
    overall_note: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.resume_playbook_request_expected_type_0 import ResumePlaybookRequestExpectedType0

        run_id = self.run_id

        human_input = self.human_input

        expected: dict[str, Any] | None | Unset
        if isinstance(self.expected, Unset):
            expected = UNSET
        elif isinstance(self.expected, ResumePlaybookRequestExpectedType0):
            expected = self.expected.to_dict()
        else:
            expected = self.expected

        preview_id: None | str | Unset
        if isinstance(self.preview_id, Unset):
            preview_id = UNSET
        else:
            preview_id = self.preview_id

        idempotency_key: None | str | Unset
        if isinstance(self.idempotency_key, Unset):
            idempotency_key = UNSET
        else:
            idempotency_key = self.idempotency_key

        overall_note: None | str | Unset
        if isinstance(self.overall_note, Unset):
            overall_note = UNSET
        else:
            overall_note = self.overall_note

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "run_id": run_id,
                "human_input": human_input,
            }
        )
        if expected is not UNSET:
            field_dict["expected"] = expected
        if preview_id is not UNSET:
            field_dict["preview_id"] = preview_id
        if idempotency_key is not UNSET:
            field_dict["idempotency_key"] = idempotency_key
        if overall_note is not UNSET:
            field_dict["overall_note"] = overall_note

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.resume_playbook_request_expected_type_0 import ResumePlaybookRequestExpectedType0

        d = dict(src_dict)
        run_id = d.pop("run_id")

        human_input = d.pop("human_input")

        def _parse_expected(data: object) -> None | ResumePlaybookRequestExpectedType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                expected_type_0 = ResumePlaybookRequestExpectedType0.from_dict(data)

                return expected_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ResumePlaybookRequestExpectedType0 | Unset, data)

        expected = _parse_expected(d.pop("expected", UNSET))

        def _parse_preview_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        preview_id = _parse_preview_id(d.pop("preview_id", UNSET))

        def _parse_idempotency_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        idempotency_key = _parse_idempotency_key(d.pop("idempotency_key", UNSET))

        def _parse_overall_note(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        overall_note = _parse_overall_note(d.pop("overall_note", UNSET))

        resume_playbook_request = cls(
            run_id=run_id,
            human_input=human_input,
            expected=expected,
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            overall_note=overall_note,
        )

        resume_playbook_request.additional_properties = d
        return resume_playbook_request

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
