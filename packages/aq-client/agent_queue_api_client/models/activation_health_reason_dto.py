from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="ActivationHealthReasonDTO")


@_attrs_define
class ActivationHealthReasonDTO:
    """
    Attributes:
        code (str):
        message (str):
        subject (None | str | Unset):
        expected_fingerprint (None | str | Unset):
        actual_fingerprint (None | str | Unset):
    """

    code: str
    message: str
    subject: None | str | Unset = UNSET
    expected_fingerprint: None | str | Unset = UNSET
    actual_fingerprint: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        code = self.code

        message = self.message

        subject: None | str | Unset
        if isinstance(self.subject, Unset):
            subject = UNSET
        else:
            subject = self.subject

        expected_fingerprint: None | str | Unset
        if isinstance(self.expected_fingerprint, Unset):
            expected_fingerprint = UNSET
        else:
            expected_fingerprint = self.expected_fingerprint

        actual_fingerprint: None | str | Unset
        if isinstance(self.actual_fingerprint, Unset):
            actual_fingerprint = UNSET
        else:
            actual_fingerprint = self.actual_fingerprint

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "code": code,
                "message": message,
            }
        )
        if subject is not UNSET:
            field_dict["subject"] = subject
        if expected_fingerprint is not UNSET:
            field_dict["expected_fingerprint"] = expected_fingerprint
        if actual_fingerprint is not UNSET:
            field_dict["actual_fingerprint"] = actual_fingerprint

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        message = d.pop("message")

        def _parse_subject(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        subject = _parse_subject(d.pop("subject", UNSET))

        def _parse_expected_fingerprint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expected_fingerprint = _parse_expected_fingerprint(d.pop("expected_fingerprint", UNSET))

        def _parse_actual_fingerprint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        actual_fingerprint = _parse_actual_fingerprint(d.pop("actual_fingerprint", UNSET))

        activation_health_reason_dto = cls(
            code=code,
            message=message,
            subject=subject,
            expected_fingerprint=expected_fingerprint,
            actual_fingerprint=actual_fingerprint,
        )

        return activation_health_reason_dto
