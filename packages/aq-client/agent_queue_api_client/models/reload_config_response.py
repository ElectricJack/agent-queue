from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReloadConfigResponse")


@_attrs_define
class ReloadConfigResponse:
    """
    Attributes:
        message (str | Unset):  Default: ''.
        changed_sections (list[str] | None | Unset):
        applied (list[str] | None | Unset):
        restart_required (list[str] | None | Unset):
        summary (None | str | Unset):
    """

    message: str | Unset = ""
    changed_sections: list[str] | None | Unset = UNSET
    applied: list[str] | None | Unset = UNSET
    restart_required: list[str] | None | Unset = UNSET
    summary: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        message = self.message

        changed_sections: list[str] | None | Unset
        if isinstance(self.changed_sections, Unset):
            changed_sections = UNSET
        elif isinstance(self.changed_sections, list):
            changed_sections = self.changed_sections

        else:
            changed_sections = self.changed_sections

        applied: list[str] | None | Unset
        if isinstance(self.applied, Unset):
            applied = UNSET
        elif isinstance(self.applied, list):
            applied = self.applied

        else:
            applied = self.applied

        restart_required: list[str] | None | Unset
        if isinstance(self.restart_required, Unset):
            restart_required = UNSET
        elif isinstance(self.restart_required, list):
            restart_required = self.restart_required

        else:
            restart_required = self.restart_required

        summary: None | str | Unset
        if isinstance(self.summary, Unset):
            summary = UNSET
        else:
            summary = self.summary

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if message is not UNSET:
            field_dict["message"] = message
        if changed_sections is not UNSET:
            field_dict["changed_sections"] = changed_sections
        if applied is not UNSET:
            field_dict["applied"] = applied
        if restart_required is not UNSET:
            field_dict["restart_required"] = restart_required
        if summary is not UNSET:
            field_dict["summary"] = summary

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        message = d.pop("message", UNSET)

        def _parse_changed_sections(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                changed_sections_type_0 = cast(list[str], data)

                return changed_sections_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        changed_sections = _parse_changed_sections(d.pop("changed_sections", UNSET))

        def _parse_applied(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                applied_type_0 = cast(list[str], data)

                return applied_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        applied = _parse_applied(d.pop("applied", UNSET))

        def _parse_restart_required(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                restart_required_type_0 = cast(list[str], data)

                return restart_required_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        restart_required = _parse_restart_required(d.pop("restart_required", UNSET))

        def _parse_summary(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        summary = _parse_summary(d.pop("summary", UNSET))

        reload_config_response = cls(
            message=message,
            changed_sections=changed_sections,
            applied=applied,
            restart_required=restart_required,
            summary=summary,
        )

        reload_config_response.additional_properties = d
        return reload_config_response

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
