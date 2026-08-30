from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SetPlaybookEnabledRequest")


@_attrs_define
class SetPlaybookEnabledRequest:
    """
    Attributes:
        playbook_id (str): The playbook identifier.
        enabled (bool): True to resume; false to pause.
        expected_source_hash (None | str | Unset): Optional optimistic-concurrency token from the last
            get_playbook_source call.
    """

    playbook_id: str
    enabled: bool
    expected_source_hash: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        enabled = self.enabled

        expected_source_hash: None | str | Unset
        if isinstance(self.expected_source_hash, Unset):
            expected_source_hash = UNSET
        else:
            expected_source_hash = self.expected_source_hash

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook_id": playbook_id,
                "enabled": enabled,
            }
        )
        if expected_source_hash is not UNSET:
            field_dict["expected_source_hash"] = expected_source_hash

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        enabled = d.pop("enabled")

        def _parse_expected_source_hash(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expected_source_hash = _parse_expected_source_hash(d.pop("expected_source_hash", UNSET))

        set_playbook_enabled_request = cls(
            playbook_id=playbook_id,
            enabled=enabled,
            expected_source_hash=expected_source_hash,
        )

        set_playbook_enabled_request.additional_properties = d
        return set_playbook_enabled_request

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
