from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.profile_config_divergence import ProfileConfigDivergence


T = TypeVar("T", bound="ProfileDriftRow")


@_attrs_define
class ProfileDriftRow:
    """One system profile's vault copy compared against the shipped default.

    Attributes:
        profile_id (str):
        status (str | Unset):  Default: 'ok'.
        config (list[ProfileConfigDivergence] | Unset):
        missing_sections (list[str] | Unset):
        extra_sections (list[str] | Unset):
        errors (list[str] | Unset):
        summary (str | Unset):  Default: ''.
    """

    profile_id: str
    status: str | Unset = "ok"
    config: list[ProfileConfigDivergence] | Unset = UNSET
    missing_sections: list[str] | Unset = UNSET
    extra_sections: list[str] | Unset = UNSET
    errors: list[str] | Unset = UNSET
    summary: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        profile_id = self.profile_id

        status = self.status

        config: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.config, Unset):
            config = []
            for config_item_data in self.config:
                config_item = config_item_data.to_dict()
                config.append(config_item)

        missing_sections: list[str] | Unset = UNSET
        if not isinstance(self.missing_sections, Unset):
            missing_sections = self.missing_sections

        extra_sections: list[str] | Unset = UNSET
        if not isinstance(self.extra_sections, Unset):
            extra_sections = self.extra_sections

        errors: list[str] | Unset = UNSET
        if not isinstance(self.errors, Unset):
            errors = self.errors

        summary = self.summary

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "profile_id": profile_id,
            }
        )
        if status is not UNSET:
            field_dict["status"] = status
        if config is not UNSET:
            field_dict["config"] = config
        if missing_sections is not UNSET:
            field_dict["missing_sections"] = missing_sections
        if extra_sections is not UNSET:
            field_dict["extra_sections"] = extra_sections
        if errors is not UNSET:
            field_dict["errors"] = errors
        if summary is not UNSET:
            field_dict["summary"] = summary

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.profile_config_divergence import ProfileConfigDivergence

        d = dict(src_dict)
        profile_id = d.pop("profile_id")

        status = d.pop("status", UNSET)

        _config = d.pop("config", UNSET)
        config: list[ProfileConfigDivergence] | Unset = UNSET
        if _config is not UNSET:
            config = []
            for config_item_data in _config:
                config_item = ProfileConfigDivergence.from_dict(config_item_data)

                config.append(config_item)

        missing_sections = cast(list[str], d.pop("missing_sections", UNSET))

        extra_sections = cast(list[str], d.pop("extra_sections", UNSET))

        errors = cast(list[str], d.pop("errors", UNSET))

        summary = d.pop("summary", UNSET)

        profile_drift_row = cls(
            profile_id=profile_id,
            status=status,
            config=config,
            missing_sections=missing_sections,
            extra_sections=extra_sections,
            errors=errors,
            summary=summary,
        )

        profile_drift_row.additional_properties = d
        return profile_drift_row

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
