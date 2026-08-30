from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.profile_detail import ProfileDetail


T = TypeVar("T", bound="ShowEffectiveProfileResponse")


@_attrs_define
class ShowEffectiveProfileResponse:
    """
    Attributes:
        project_id (str):
        agent_type (str):
        profile (None | ProfileDetail | Unset):
        source (None | str | Unset):
    """

    project_id: str
    agent_type: str
    profile: None | ProfileDetail | Unset = UNSET
    source: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.profile_detail import ProfileDetail

        project_id = self.project_id

        agent_type = self.agent_type

        profile: dict[str, Any] | None | Unset
        if isinstance(self.profile, Unset):
            profile = UNSET
        elif isinstance(self.profile, ProfileDetail):
            profile = self.profile.to_dict()
        else:
            profile = self.profile

        source: None | str | Unset
        if isinstance(self.source, Unset):
            source = UNSET
        else:
            source = self.source

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "agent_type": agent_type,
            }
        )
        if profile is not UNSET:
            field_dict["profile"] = profile
        if source is not UNSET:
            field_dict["source"] = source

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.profile_detail import ProfileDetail

        d = dict(src_dict)
        project_id = d.pop("project_id")

        agent_type = d.pop("agent_type")

        def _parse_profile(data: object) -> None | ProfileDetail | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                profile_type_0 = ProfileDetail.from_dict(data)

                return profile_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProfileDetail | Unset, data)

        profile = _parse_profile(d.pop("profile", UNSET))

        def _parse_source(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source = _parse_source(d.pop("source", UNSET))

        show_effective_profile_response = cls(
            project_id=project_id,
            agent_type=agent_type,
            profile=profile,
            source=source,
        )

        show_effective_profile_response.additional_properties = d
        return show_effective_profile_response

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
