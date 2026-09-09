from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.digest_escalation_settings import DigestEscalationSettings
    from ..models.digest_schedule_settings import DigestScheduleSettings
    from ..models.digest_status_response_delivery_health import DigestStatusResponseDeliveryHealth
    from ..models.digest_window_record import DigestWindowRecord
    from ..models.discord_cutover_status import DiscordCutoverStatus


T = TypeVar("T", bound="DigestStatusResponse")


@_attrs_define
class DigestStatusResponse:
    """
    Attributes:
        destination (str):
        config_generation (int):
        digest (DigestScheduleSettings):
        escalation (DigestEscalationSettings):
        next_evaluation_at (float):
        success (bool | Unset):  Default: True.
        channel_id (str | Unset):  Default: ''.
        last_window_end (float | None | Unset):
        recent_windows (list[DigestWindowRecord] | Unset):
        delivery_health (DigestStatusResponseDeliveryHealth | Unset):
        open_escalations (int | Unset):  Default: 0.
        pending_escalation_deliveries (int | Unset):  Default: 0.
        cutover (DiscordCutoverStatus | None | Unset):
        settings_errors (list[str] | Unset):
        warnings (list[str] | Unset):
    """

    destination: str
    config_generation: int
    digest: DigestScheduleSettings
    escalation: DigestEscalationSettings
    next_evaluation_at: float
    success: bool | Unset = True
    channel_id: str | Unset = ""
    last_window_end: float | None | Unset = UNSET
    recent_windows: list[DigestWindowRecord] | Unset = UNSET
    delivery_health: DigestStatusResponseDeliveryHealth | Unset = UNSET
    open_escalations: int | Unset = 0
    pending_escalation_deliveries: int | Unset = 0
    cutover: DiscordCutoverStatus | None | Unset = UNSET
    settings_errors: list[str] | Unset = UNSET
    warnings: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.discord_cutover_status import DiscordCutoverStatus

        destination = self.destination

        config_generation = self.config_generation

        digest = self.digest.to_dict()

        escalation = self.escalation.to_dict()

        next_evaluation_at = self.next_evaluation_at

        success = self.success

        channel_id = self.channel_id

        last_window_end: float | None | Unset
        if isinstance(self.last_window_end, Unset):
            last_window_end = UNSET
        else:
            last_window_end = self.last_window_end

        recent_windows: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.recent_windows, Unset):
            recent_windows = []
            for recent_windows_item_data in self.recent_windows:
                recent_windows_item = recent_windows_item_data.to_dict()
                recent_windows.append(recent_windows_item)

        delivery_health: dict[str, Any] | Unset = UNSET
        if not isinstance(self.delivery_health, Unset):
            delivery_health = self.delivery_health.to_dict()

        open_escalations = self.open_escalations

        pending_escalation_deliveries = self.pending_escalation_deliveries

        cutover: dict[str, Any] | None | Unset
        if isinstance(self.cutover, Unset):
            cutover = UNSET
        elif isinstance(self.cutover, DiscordCutoverStatus):
            cutover = self.cutover.to_dict()
        else:
            cutover = self.cutover

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
                "digest": digest,
                "escalation": escalation,
                "next_evaluation_at": next_evaluation_at,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if channel_id is not UNSET:
            field_dict["channel_id"] = channel_id
        if last_window_end is not UNSET:
            field_dict["last_window_end"] = last_window_end
        if recent_windows is not UNSET:
            field_dict["recent_windows"] = recent_windows
        if delivery_health is not UNSET:
            field_dict["delivery_health"] = delivery_health
        if open_escalations is not UNSET:
            field_dict["open_escalations"] = open_escalations
        if pending_escalation_deliveries is not UNSET:
            field_dict["pending_escalation_deliveries"] = pending_escalation_deliveries
        if cutover is not UNSET:
            field_dict["cutover"] = cutover
        if settings_errors is not UNSET:
            field_dict["settings_errors"] = settings_errors
        if warnings is not UNSET:
            field_dict["warnings"] = warnings

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.digest_escalation_settings import DigestEscalationSettings
        from ..models.digest_schedule_settings import DigestScheduleSettings
        from ..models.digest_status_response_delivery_health import DigestStatusResponseDeliveryHealth
        from ..models.digest_window_record import DigestWindowRecord
        from ..models.discord_cutover_status import DiscordCutoverStatus

        d = dict(src_dict)
        destination = d.pop("destination")

        config_generation = d.pop("config_generation")

        digest = DigestScheduleSettings.from_dict(d.pop("digest"))

        escalation = DigestEscalationSettings.from_dict(d.pop("escalation"))

        next_evaluation_at = d.pop("next_evaluation_at")

        success = d.pop("success", UNSET)

        channel_id = d.pop("channel_id", UNSET)

        def _parse_last_window_end(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        last_window_end = _parse_last_window_end(d.pop("last_window_end", UNSET))

        _recent_windows = d.pop("recent_windows", UNSET)
        recent_windows: list[DigestWindowRecord] | Unset = UNSET
        if _recent_windows is not UNSET:
            recent_windows = []
            for recent_windows_item_data in _recent_windows:
                recent_windows_item = DigestWindowRecord.from_dict(recent_windows_item_data)

                recent_windows.append(recent_windows_item)

        _delivery_health = d.pop("delivery_health", UNSET)
        delivery_health: DigestStatusResponseDeliveryHealth | Unset
        if isinstance(_delivery_health, Unset):
            delivery_health = UNSET
        else:
            delivery_health = DigestStatusResponseDeliveryHealth.from_dict(_delivery_health)

        open_escalations = d.pop("open_escalations", UNSET)

        pending_escalation_deliveries = d.pop("pending_escalation_deliveries", UNSET)

        def _parse_cutover(data: object) -> DiscordCutoverStatus | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                cutover_type_0 = DiscordCutoverStatus.from_dict(data)

                return cutover_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(DiscordCutoverStatus | None | Unset, data)

        cutover = _parse_cutover(d.pop("cutover", UNSET))

        settings_errors = cast(list[str], d.pop("settings_errors", UNSET))

        warnings = cast(list[str], d.pop("warnings", UNSET))

        digest_status_response = cls(
            destination=destination,
            config_generation=config_generation,
            digest=digest,
            escalation=escalation,
            next_evaluation_at=next_evaluation_at,
            success=success,
            channel_id=channel_id,
            last_window_end=last_window_end,
            recent_windows=recent_windows,
            delivery_health=delivery_health,
            open_escalations=open_escalations,
            pending_escalation_deliveries=pending_escalation_deliveries,
            cutover=cutover,
            settings_errors=settings_errors,
            warnings=warnings,
        )

        digest_status_response.additional_properties = d
        return digest_status_response

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
