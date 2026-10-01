from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.git_identity_value import GitIdentityValue


T = TypeVar("T", bound="SetGitIdentityResponse")


@_attrs_define
class SetGitIdentityResponse:
    """
    Attributes:
        configured (bool):
        success (bool | Unset):  Default: True.
        installation (GitIdentityValue | None | Unset):
        previous (GitIdentityValue | None | Unset):
        changed (bool | Unset):  Default: False.
        applies_to (str | Unset):  Default: ''.
    """

    configured: bool
    success: bool | Unset = True
    installation: GitIdentityValue | None | Unset = UNSET
    previous: GitIdentityValue | None | Unset = UNSET
    changed: bool | Unset = False
    applies_to: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.git_identity_value import GitIdentityValue

        configured = self.configured

        success = self.success

        installation: dict[str, Any] | None | Unset
        if isinstance(self.installation, Unset):
            installation = UNSET
        elif isinstance(self.installation, GitIdentityValue):
            installation = self.installation.to_dict()
        else:
            installation = self.installation

        previous: dict[str, Any] | None | Unset
        if isinstance(self.previous, Unset):
            previous = UNSET
        elif isinstance(self.previous, GitIdentityValue):
            previous = self.previous.to_dict()
        else:
            previous = self.previous

        changed = self.changed

        applies_to = self.applies_to

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "configured": configured,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if installation is not UNSET:
            field_dict["installation"] = installation
        if previous is not UNSET:
            field_dict["previous"] = previous
        if changed is not UNSET:
            field_dict["changed"] = changed
        if applies_to is not UNSET:
            field_dict["applies_to"] = applies_to

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.git_identity_value import GitIdentityValue

        d = dict(src_dict)
        configured = d.pop("configured")

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

        def _parse_previous(data: object) -> GitIdentityValue | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                previous_type_0 = GitIdentityValue.from_dict(data)

                return previous_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(GitIdentityValue | None | Unset, data)

        previous = _parse_previous(d.pop("previous", UNSET))

        changed = d.pop("changed", UNSET)

        applies_to = d.pop("applies_to", UNSET)

        set_git_identity_response = cls(
            configured=configured,
            success=success,
            installation=installation,
            previous=previous,
            changed=changed,
            applies_to=applies_to,
        )

        set_git_identity_response.additional_properties = d
        return set_git_identity_response

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
