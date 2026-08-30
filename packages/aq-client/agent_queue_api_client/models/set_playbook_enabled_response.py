from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SetPlaybookEnabledResponse")


@_attrs_define
class SetPlaybookEnabledResponse:
    """
    Attributes:
        playbook_id (str):
        enabled (bool):
        compiled (bool | Unset):  Default: False.
        noop (bool | Unset):  Default: False.
        source_hash (None | str | Unset):
        errors (list[str] | None | Unset):
    """

    playbook_id: str
    enabled: bool
    compiled: bool | Unset = False
    noop: bool | Unset = False
    source_hash: None | str | Unset = UNSET
    errors: list[str] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        enabled = self.enabled

        compiled = self.compiled

        noop = self.noop

        source_hash: None | str | Unset
        if isinstance(self.source_hash, Unset):
            source_hash = UNSET
        else:
            source_hash = self.source_hash

        errors: list[str] | None | Unset
        if isinstance(self.errors, Unset):
            errors = UNSET
        elif isinstance(self.errors, list):
            errors = self.errors

        else:
            errors = self.errors

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook_id": playbook_id,
                "enabled": enabled,
            }
        )
        if compiled is not UNSET:
            field_dict["compiled"] = compiled
        if noop is not UNSET:
            field_dict["noop"] = noop
        if source_hash is not UNSET:
            field_dict["source_hash"] = source_hash
        if errors is not UNSET:
            field_dict["errors"] = errors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        enabled = d.pop("enabled")

        compiled = d.pop("compiled", UNSET)

        noop = d.pop("noop", UNSET)

        def _parse_source_hash(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source_hash = _parse_source_hash(d.pop("source_hash", UNSET))

        def _parse_errors(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                errors_type_0 = cast(list[str], data)

                return errors_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        errors = _parse_errors(d.pop("errors", UNSET))

        set_playbook_enabled_response = cls(
            playbook_id=playbook_id,
            enabled=enabled,
            compiled=compiled,
            noop=noop,
            source_hash=source_hash,
            errors=errors,
        )

        set_playbook_enabled_response.additional_properties = d
        return set_playbook_enabled_response

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
