from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PlaybookActivateRequest")


@_attrs_define
class PlaybookActivateRequest:
    """
    Attributes:
        playbook_id (str): The playbook to activate against.
        artifact_sha256 (str): The reviewed artifact hash, full 'sha256:<64 hex>' form.
        enabled (bool | Unset): Whether the activation is enabled. Default: true. Default: True.
        acknowledge_diff (None | str | Unset): Required when the diff against the active artifact is executable. Must
            equal artifact_sha256, so an acknowledgement cannot be replayed against another artifact.
    """

    playbook_id: str
    artifact_sha256: str
    enabled: bool | Unset = True
    acknowledge_diff: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        artifact_sha256 = self.artifact_sha256

        enabled = self.enabled

        acknowledge_diff: None | str | Unset
        if isinstance(self.acknowledge_diff, Unset):
            acknowledge_diff = UNSET
        else:
            acknowledge_diff = self.acknowledge_diff

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook_id": playbook_id,
                "artifact_sha256": artifact_sha256,
            }
        )
        if enabled is not UNSET:
            field_dict["enabled"] = enabled
        if acknowledge_diff is not UNSET:
            field_dict["acknowledge_diff"] = acknowledge_diff

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        artifact_sha256 = d.pop("artifact_sha256")

        enabled = d.pop("enabled", UNSET)

        def _parse_acknowledge_diff(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        acknowledge_diff = _parse_acknowledge_diff(d.pop("acknowledge_diff", UNSET))

        playbook_activate_request = cls(
            playbook_id=playbook_id,
            artifact_sha256=artifact_sha256,
            enabled=enabled,
            acknowledge_diff=acknowledge_diff,
        )

        playbook_activate_request.additional_properties = d
        return playbook_activate_request

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
