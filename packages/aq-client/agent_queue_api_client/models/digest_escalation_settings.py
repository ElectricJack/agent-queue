from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DigestEscalationSettings")


@_attrs_define
class DigestEscalationSettings:
    """
    Attributes:
        enabled (bool):
        reminder_minutes (int):
        supervisor_delivery_timeout_minutes (int):
        mention_user_ids (list[str] | Unset):
        mention_role_ids (list[str] | Unset):
    """

    enabled: bool
    reminder_minutes: int
    supervisor_delivery_timeout_minutes: int
    mention_user_ids: list[str] | Unset = UNSET
    mention_role_ids: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        enabled = self.enabled

        reminder_minutes = self.reminder_minutes

        supervisor_delivery_timeout_minutes = self.supervisor_delivery_timeout_minutes

        mention_user_ids: list[str] | Unset = UNSET
        if not isinstance(self.mention_user_ids, Unset):
            mention_user_ids = self.mention_user_ids

        mention_role_ids: list[str] | Unset = UNSET
        if not isinstance(self.mention_role_ids, Unset):
            mention_role_ids = self.mention_role_ids

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "enabled": enabled,
                "reminder_minutes": reminder_minutes,
                "supervisor_delivery_timeout_minutes": supervisor_delivery_timeout_minutes,
            }
        )
        if mention_user_ids is not UNSET:
            field_dict["mention_user_ids"] = mention_user_ids
        if mention_role_ids is not UNSET:
            field_dict["mention_role_ids"] = mention_role_ids

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        enabled = d.pop("enabled")

        reminder_minutes = d.pop("reminder_minutes")

        supervisor_delivery_timeout_minutes = d.pop("supervisor_delivery_timeout_minutes")

        mention_user_ids = cast(list[str], d.pop("mention_user_ids", UNSET))

        mention_role_ids = cast(list[str], d.pop("mention_role_ids", UNSET))

        digest_escalation_settings = cls(
            enabled=enabled,
            reminder_minutes=reminder_minutes,
            supervisor_delivery_timeout_minutes=supervisor_delivery_timeout_minutes,
            mention_user_ids=mention_user_ids,
            mention_role_ids=mention_role_ids,
        )

        digest_escalation_settings.additional_properties = d
        return digest_escalation_settings

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
