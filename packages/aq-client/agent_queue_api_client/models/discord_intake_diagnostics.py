from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.discord_intake_diagnostics_ignored import DiscordIntakeDiagnosticsIgnored


T = TypeVar("T", bound="DiscordIntakeDiagnostics")


@_attrs_define
class DiscordIntakeDiagnostics:
    """Inbound Discord messages the gateway ignored, counted by reason code.

    In-memory and sliding: it covers the last ``window_seconds`` and is empty
    after a restart.  ``available`` is false when no gateway is connected.

        Attributes:
            available (bool | Unset):  Default: False.
            window_seconds (int | Unset):  Default: 3600.
            total (int | Unset):  Default: 0.
            ignored (DiscordIntakeDiagnosticsIgnored | Unset):
    """

    available: bool | Unset = False
    window_seconds: int | Unset = 3600
    total: int | Unset = 0
    ignored: DiscordIntakeDiagnosticsIgnored | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        available = self.available

        window_seconds = self.window_seconds

        total = self.total

        ignored: dict[str, Any] | Unset = UNSET
        if not isinstance(self.ignored, Unset):
            ignored = self.ignored.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if available is not UNSET:
            field_dict["available"] = available
        if window_seconds is not UNSET:
            field_dict["window_seconds"] = window_seconds
        if total is not UNSET:
            field_dict["total"] = total
        if ignored is not UNSET:
            field_dict["ignored"] = ignored

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.discord_intake_diagnostics_ignored import DiscordIntakeDiagnosticsIgnored

        d = dict(src_dict)
        available = d.pop("available", UNSET)

        window_seconds = d.pop("window_seconds", UNSET)

        total = d.pop("total", UNSET)

        _ignored = d.pop("ignored", UNSET)
        ignored: DiscordIntakeDiagnosticsIgnored | Unset
        if isinstance(_ignored, Unset):
            ignored = UNSET
        else:
            ignored = DiscordIntakeDiagnosticsIgnored.from_dict(_ignored)

        discord_intake_diagnostics = cls(
            available=available,
            window_seconds=window_seconds,
            total=total,
            ignored=ignored,
        )

        discord_intake_diagnostics.additional_properties = d
        return discord_intake_diagnostics

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
