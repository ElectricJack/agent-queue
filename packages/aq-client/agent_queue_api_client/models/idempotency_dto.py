from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="IdempotencyDTO")


@_attrs_define
class IdempotencyDTO:
    """
    Attributes:
        supported (bool | Unset):  Default: False.
        key_template (None | str | Unset):
        retry_safe (bool | Unset):  Default: False.
    """

    supported: bool | Unset = False
    key_template: None | str | Unset = UNSET
    retry_safe: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        supported = self.supported

        key_template: None | str | Unset
        if isinstance(self.key_template, Unset):
            key_template = UNSET
        else:
            key_template = self.key_template

        retry_safe = self.retry_safe

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if supported is not UNSET:
            field_dict["supported"] = supported
        if key_template is not UNSET:
            field_dict["key_template"] = key_template
        if retry_safe is not UNSET:
            field_dict["retry_safe"] = retry_safe

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        supported = d.pop("supported", UNSET)

        def _parse_key_template(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        key_template = _parse_key_template(d.pop("key_template", UNSET))

        retry_safe = d.pop("retry_safe", UNSET)

        idempotency_dto = cls(
            supported=supported,
            key_template=key_template,
            retry_safe=retry_safe,
        )

        return idempotency_dto
