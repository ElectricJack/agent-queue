from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderAllocationWarning")


@_attrs_define
class ProviderAllocationWarning:
    """``pinned_ready_wait`` (blocking), ``manual_agent_push_changes``, ``bounds_skipped``,
    ``no_change``.

        Attributes:
            code (str):
            message (str):
            blocking (bool | Unset):  Default: False.
            acknowledged (bool | Unset):  Default: False.
            subjects (list[str] | Unset):
    """

    code: str
    message: str
    blocking: bool | Unset = False
    acknowledged: bool | Unset = False
    subjects: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        code = self.code

        message = self.message

        blocking = self.blocking

        acknowledged = self.acknowledged

        subjects: list[str] | Unset = UNSET
        if not isinstance(self.subjects, Unset):
            subjects = self.subjects

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "code": code,
                "message": message,
            }
        )
        if blocking is not UNSET:
            field_dict["blocking"] = blocking
        if acknowledged is not UNSET:
            field_dict["acknowledged"] = acknowledged
        if subjects is not UNSET:
            field_dict["subjects"] = subjects

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        message = d.pop("message")

        blocking = d.pop("blocking", UNSET)

        acknowledged = d.pop("acknowledged", UNSET)

        subjects = cast(list[str], d.pop("subjects", UNSET))

        provider_allocation_warning = cls(
            code=code,
            message=message,
            blocking=blocking,
            acknowledged=acknowledged,
            subjects=subjects,
        )

        provider_allocation_warning.additional_properties = d
        return provider_allocation_warning

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
