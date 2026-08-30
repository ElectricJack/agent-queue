from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.profile_detail import ProfileDetail


T = TypeVar("T", bound="ProjectProfileRow")


@_attrs_define
class ProjectProfileRow:
    """
    Attributes:
        agent_type (str):
        global_ (None | ProfileDetail | Unset):
        scoped (None | ProfileDetail | Unset):
        effective (None | ProfileDetail | Unset):
        has_override (bool | Unset):  Default: False.
    """

    agent_type: str
    global_: None | ProfileDetail | Unset = UNSET
    scoped: None | ProfileDetail | Unset = UNSET
    effective: None | ProfileDetail | Unset = UNSET
    has_override: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.profile_detail import ProfileDetail

        agent_type = self.agent_type

        global_: dict[str, Any] | None | Unset
        if isinstance(self.global_, Unset):
            global_ = UNSET
        elif isinstance(self.global_, ProfileDetail):
            global_ = self.global_.to_dict()
        else:
            global_ = self.global_

        scoped: dict[str, Any] | None | Unset
        if isinstance(self.scoped, Unset):
            scoped = UNSET
        elif isinstance(self.scoped, ProfileDetail):
            scoped = self.scoped.to_dict()
        else:
            scoped = self.scoped

        effective: dict[str, Any] | None | Unset
        if isinstance(self.effective, Unset):
            effective = UNSET
        elif isinstance(self.effective, ProfileDetail):
            effective = self.effective.to_dict()
        else:
            effective = self.effective

        has_override = self.has_override

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "agent_type": agent_type,
            }
        )
        if global_ is not UNSET:
            field_dict["global"] = global_
        if scoped is not UNSET:
            field_dict["scoped"] = scoped
        if effective is not UNSET:
            field_dict["effective"] = effective
        if has_override is not UNSET:
            field_dict["has_override"] = has_override

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.profile_detail import ProfileDetail

        d = dict(src_dict)
        agent_type = d.pop("agent_type")

        def _parse_global_(data: object) -> None | ProfileDetail | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                global_type_0 = ProfileDetail.from_dict(data)

                return global_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProfileDetail | Unset, data)

        global_ = _parse_global_(d.pop("global", UNSET))

        def _parse_scoped(data: object) -> None | ProfileDetail | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                scoped_type_0 = ProfileDetail.from_dict(data)

                return scoped_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProfileDetail | Unset, data)

        scoped = _parse_scoped(d.pop("scoped", UNSET))

        def _parse_effective(data: object) -> None | ProfileDetail | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                effective_type_0 = ProfileDetail.from_dict(data)

                return effective_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProfileDetail | Unset, data)

        effective = _parse_effective(d.pop("effective", UNSET))

        has_override = d.pop("has_override", UNSET)

        project_profile_row = cls(
            agent_type=agent_type,
            global_=global_,
            scoped=scoped,
            effective=effective,
            has_override=has_override,
        )

        project_profile_row.additional_properties = d
        return project_profile_row

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
