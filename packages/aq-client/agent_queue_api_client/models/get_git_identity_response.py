from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.effective_git_identity import EffectiveGitIdentity
    from ..models.git_identity_value import GitIdentityValue


T = TypeVar("T", bound="GetGitIdentityResponse")


@_attrs_define
class GetGitIdentityResponse:
    """The installation default, the fallback, and optionally one project's effective identity.

    Attributes:
        configured (bool):
        fallback (GitIdentityValue):
        success (bool | Unset):  Default: True.
        installation (GitIdentityValue | None | Unset):
        installation_source (None | str | Unset):
        project_id (None | str | Unset):
        effective (EffectiveGitIdentity | None | Unset):
    """

    configured: bool
    fallback: GitIdentityValue
    success: bool | Unset = True
    installation: GitIdentityValue | None | Unset = UNSET
    installation_source: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    effective: EffectiveGitIdentity | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.effective_git_identity import EffectiveGitIdentity
        from ..models.git_identity_value import GitIdentityValue

        configured = self.configured

        fallback = self.fallback.to_dict()

        success = self.success

        installation: dict[str, Any] | None | Unset
        if isinstance(self.installation, Unset):
            installation = UNSET
        elif isinstance(self.installation, GitIdentityValue):
            installation = self.installation.to_dict()
        else:
            installation = self.installation

        installation_source: None | str | Unset
        if isinstance(self.installation_source, Unset):
            installation_source = UNSET
        else:
            installation_source = self.installation_source

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        effective: dict[str, Any] | None | Unset
        if isinstance(self.effective, Unset):
            effective = UNSET
        elif isinstance(self.effective, EffectiveGitIdentity):
            effective = self.effective.to_dict()
        else:
            effective = self.effective

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "configured": configured,
                "fallback": fallback,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if installation is not UNSET:
            field_dict["installation"] = installation
        if installation_source is not UNSET:
            field_dict["installation_source"] = installation_source
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if effective is not UNSET:
            field_dict["effective"] = effective

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.effective_git_identity import EffectiveGitIdentity
        from ..models.git_identity_value import GitIdentityValue

        d = dict(src_dict)
        configured = d.pop("configured")

        fallback = GitIdentityValue.from_dict(d.pop("fallback"))

        success = d.pop("success", UNSET)

        def _parse_installation(data: object) -> GitIdentityValue | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                installation_type_0 = GitIdentityValue.from_dict(data)

                return installation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(GitIdentityValue | None | Unset, data)

        installation = _parse_installation(d.pop("installation", UNSET))

        def _parse_installation_source(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        installation_source = _parse_installation_source(d.pop("installation_source", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_effective(data: object) -> EffectiveGitIdentity | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                effective_type_0 = EffectiveGitIdentity.from_dict(data)

                return effective_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EffectiveGitIdentity | None | Unset, data)

        effective = _parse_effective(d.pop("effective", UNSET))

        get_git_identity_response = cls(
            configured=configured,
            fallback=fallback,
            success=success,
            installation=installation,
            installation_source=installation_source,
            project_id=project_id,
            effective=effective,
        )

        get_git_identity_response.additional_properties = d
        return get_git_identity_response

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
