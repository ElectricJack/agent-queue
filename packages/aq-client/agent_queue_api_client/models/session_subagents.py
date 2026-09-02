from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SessionSubagents")


@_attrs_define
class SessionSubagents:
    """One live session's children.

    ``hooks`` is False when the session was launched without its harness
    sub-agent hooks wired, which makes ``native`` a floor rather than a total.

        Attributes:
            native (float | Unset):  Default: 0.0.
            aq (float | Unset):  Default: 0.0.
            hooks (bool | Unset):  Default: True.
    """

    native: float | Unset = 0.0
    aq: float | Unset = 0.0
    hooks: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        native = self.native

        aq = self.aq

        hooks = self.hooks

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if native is not UNSET:
            field_dict["native"] = native
        if aq is not UNSET:
            field_dict["aq"] = aq
        if hooks is not UNSET:
            field_dict["hooks"] = hooks

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        native = d.pop("native", UNSET)

        aq = d.pop("aq", UNSET)

        hooks = d.pop("hooks", UNSET)

        session_subagents = cls(
            native=native,
            aq=aq,
            hooks=hooks,
        )

        session_subagents.additional_properties = d
        return session_subagents

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
