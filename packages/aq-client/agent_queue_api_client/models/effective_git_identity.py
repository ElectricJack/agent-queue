from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.effective_git_identity_source import EffectiveGitIdentitySource
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.git_identity_pair import GitIdentityPair


T = TypeVar("T", bound="EffectiveGitIdentity")


@_attrs_define
class EffectiveGitIdentity:
    """The identity a project's AQ-authored commits use, and where it came from.

    ``source`` is ``project`` (its override), ``installation`` (the
    ``git_identity`` default) or ``fallback`` (no default chosen yet;
    ``configured`` is then false).

        Attributes:
            name (str):
            email (str):
            source (EffectiveGitIdentitySource):
            configured (bool):
            fallback (GitIdentityPair):
            installation (GitIdentityPair | None | Unset):
            project_override (GitIdentityPair | None | Unset):
    """

    name: str
    email: str
    source: EffectiveGitIdentitySource
    configured: bool
    fallback: GitIdentityPair
    installation: GitIdentityPair | None | Unset = UNSET
    project_override: GitIdentityPair | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.git_identity_pair import GitIdentityPair

        name = self.name

        email = self.email

        source = self.source.value

        configured = self.configured

        fallback = self.fallback.to_dict()

        installation: dict[str, Any] | None | Unset
        if isinstance(self.installation, Unset):
            installation = UNSET
        elif isinstance(self.installation, GitIdentityPair):
            installation = self.installation.to_dict()
        else:
            installation = self.installation

        project_override: dict[str, Any] | None | Unset
        if isinstance(self.project_override, Unset):
            project_override = UNSET
        elif isinstance(self.project_override, GitIdentityPair):
            project_override = self.project_override.to_dict()
        else:
            project_override = self.project_override

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
                "email": email,
                "source": source,
                "configured": configured,
                "fallback": fallback,
            }
        )
        if installation is not UNSET:
            field_dict["installation"] = installation
        if project_override is not UNSET:
            field_dict["project_override"] = project_override

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.git_identity_pair import GitIdentityPair

        d = dict(src_dict)
        name = d.pop("name")

        email = d.pop("email")

        source = EffectiveGitIdentitySource(d.pop("source"))

        configured = d.pop("configured")

        fallback = GitIdentityPair.from_dict(d.pop("fallback"))

        def _parse_installation(data: object) -> GitIdentityPair | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                installation_type_0 = GitIdentityPair.from_dict(data)

                return installation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(GitIdentityPair | None | Unset, data)

        installation = _parse_installation(d.pop("installation", UNSET))

        def _parse_project_override(data: object) -> GitIdentityPair | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                project_override_type_0 = GitIdentityPair.from_dict(data)

                return project_override_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(GitIdentityPair | None | Unset, data)

        project_override = _parse_project_override(d.pop("project_override", UNSET))

        effective_git_identity = cls(
            name=name,
            email=email,
            source=source,
            configured=configured,
            fallback=fallback,
            installation=installation,
            project_override=project_override,
        )

        effective_git_identity.additional_properties = d
        return effective_git_identity

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
