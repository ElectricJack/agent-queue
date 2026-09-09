from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.digest_window_bounds import DigestWindowBounds


T = TypeVar("T", bound="DigestPreviewResponse")


@_attrs_define
class DigestPreviewResponse:
    """A dry evaluation of the current window; nothing was sent.

    Attributes:
        destination (str):
        config_generation (int):
        enabled (bool):
        window (DigestWindowBounds):
        would_send (bool):
        reason (str):
        success (bool | Unset):  Default: True.
        text (str | Unset):  Default: ''.
        completed_count (int | Unset):  Default: 0.
        active_count (int | Unset):  Default: 0.
        idle_tasks (int | Unset):  Default: 0.
        open_escalations (int | Unset):  Default: 0.
        suppression_reason (None | str | Unset):
        settings_errors (list[str] | Unset):
        warnings (list[str] | Unset):
    """

    destination: str
    config_generation: int
    enabled: bool
    window: DigestWindowBounds
    would_send: bool
    reason: str
    success: bool | Unset = True
    text: str | Unset = ""
    completed_count: int | Unset = 0
    active_count: int | Unset = 0
    idle_tasks: int | Unset = 0
    open_escalations: int | Unset = 0
    suppression_reason: None | str | Unset = UNSET
    settings_errors: list[str] | Unset = UNSET
    warnings: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        destination = self.destination

        config_generation = self.config_generation

        enabled = self.enabled

        window = self.window.to_dict()

        would_send = self.would_send

        reason = self.reason

        success = self.success

        text = self.text

        completed_count = self.completed_count

        active_count = self.active_count

        idle_tasks = self.idle_tasks

        open_escalations = self.open_escalations

        suppression_reason: None | str | Unset
        if isinstance(self.suppression_reason, Unset):
            suppression_reason = UNSET
        else:
            suppression_reason = self.suppression_reason

        settings_errors: list[str] | Unset = UNSET
        if not isinstance(self.settings_errors, Unset):
            settings_errors = self.settings_errors

        warnings: list[str] | Unset = UNSET
        if not isinstance(self.warnings, Unset):
            warnings = self.warnings

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "destination": destination,
                "config_generation": config_generation,
                "enabled": enabled,
                "window": window,
                "would_send": would_send,
                "reason": reason,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if text is not UNSET:
            field_dict["text"] = text
        if completed_count is not UNSET:
            field_dict["completed_count"] = completed_count
        if active_count is not UNSET:
            field_dict["active_count"] = active_count
        if idle_tasks is not UNSET:
            field_dict["idle_tasks"] = idle_tasks
        if open_escalations is not UNSET:
            field_dict["open_escalations"] = open_escalations
        if suppression_reason is not UNSET:
            field_dict["suppression_reason"] = suppression_reason
        if settings_errors is not UNSET:
            field_dict["settings_errors"] = settings_errors
        if warnings is not UNSET:
            field_dict["warnings"] = warnings

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.digest_window_bounds import DigestWindowBounds

        d = dict(src_dict)
        destination = d.pop("destination")

        config_generation = d.pop("config_generation")

        enabled = d.pop("enabled")

        window = DigestWindowBounds.from_dict(d.pop("window"))

        would_send = d.pop("would_send")

        reason = d.pop("reason")

        success = d.pop("success", UNSET)

        text = d.pop("text", UNSET)

        completed_count = d.pop("completed_count", UNSET)

        active_count = d.pop("active_count", UNSET)

        idle_tasks = d.pop("idle_tasks", UNSET)

        open_escalations = d.pop("open_escalations", UNSET)

        def _parse_suppression_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        suppression_reason = _parse_suppression_reason(d.pop("suppression_reason", UNSET))

        settings_errors = cast(list[str], d.pop("settings_errors", UNSET))

        warnings = cast(list[str], d.pop("warnings", UNSET))

        digest_preview_response = cls(
            destination=destination,
            config_generation=config_generation,
            enabled=enabled,
            window=window,
            would_send=would_send,
            reason=reason,
            success=success,
            text=text,
            completed_count=completed_count,
            active_count=active_count,
            idle_tasks=idle_tasks,
            open_escalations=open_escalations,
            suppression_reason=suppression_reason,
            settings_errors=settings_errors,
            warnings=warnings,
        )

        digest_preview_response.additional_properties = d
        return digest_preview_response

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
