from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.check_profile_response_manifest import CheckProfileResponseManifest


T = TypeVar("T", bound="CheckProfileResponse")


@_attrs_define
class CheckProfileResponse:
    """
    Attributes:
        profile_id (str):
        valid (bool | Unset):  Default: False.
        issues (list[str] | Unset):
        manifest (CheckProfileResponseManifest | Unset):
    """

    profile_id: str
    valid: bool | Unset = False
    issues: list[str] | Unset = UNSET
    manifest: CheckProfileResponseManifest | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        profile_id = self.profile_id

        valid = self.valid

        issues: list[str] | Unset = UNSET
        if not isinstance(self.issues, Unset):
            issues = self.issues

        manifest: dict[str, Any] | Unset = UNSET
        if not isinstance(self.manifest, Unset):
            manifest = self.manifest.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "profile_id": profile_id,
            }
        )
        if valid is not UNSET:
            field_dict["valid"] = valid
        if issues is not UNSET:
            field_dict["issues"] = issues
        if manifest is not UNSET:
            field_dict["manifest"] = manifest

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.check_profile_response_manifest import CheckProfileResponseManifest

        d = dict(src_dict)
        profile_id = d.pop("profile_id")

        valid = d.pop("valid", UNSET)

        issues = cast(list[str], d.pop("issues", UNSET))

        _manifest = d.pop("manifest", UNSET)
        manifest: CheckProfileResponseManifest | Unset
        if isinstance(_manifest, Unset):
            manifest = UNSET
        else:
            manifest = CheckProfileResponseManifest.from_dict(_manifest)

        check_profile_response = cls(
            profile_id=profile_id,
            valid=valid,
            issues=issues,
            manifest=manifest,
        )

        check_profile_response.additional_properties = d
        return check_profile_response

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
