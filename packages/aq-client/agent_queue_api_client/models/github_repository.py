from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.github_repository_visibility import GithubRepositoryVisibility
from ..types import UNSET, Unset

T = TypeVar("T", bound="GithubRepository")


@_attrs_define
class GithubRepository:
    """
    Attributes:
        owner (str):
        name (str):
        full_name (str):
        clone_url_https (str):
        visibility (GithubRepositoryVisibility | Unset):  Default: GithubRepositoryVisibility.PRIVATE.
        clone_url_ssh (None | str | Unset):
        default_branch (None | str | Unset):
        description (None | str | Unset):
    """

    owner: str
    name: str
    full_name: str
    clone_url_https: str
    visibility: GithubRepositoryVisibility | Unset = GithubRepositoryVisibility.PRIVATE
    clone_url_ssh: None | str | Unset = UNSET
    default_branch: None | str | Unset = UNSET
    description: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        owner = self.owner

        name = self.name

        full_name = self.full_name

        clone_url_https = self.clone_url_https

        visibility: str | Unset = UNSET
        if not isinstance(self.visibility, Unset):
            visibility = self.visibility.value

        clone_url_ssh: None | str | Unset
        if isinstance(self.clone_url_ssh, Unset):
            clone_url_ssh = UNSET
        else:
            clone_url_ssh = self.clone_url_ssh

        default_branch: None | str | Unset
        if isinstance(self.default_branch, Unset):
            default_branch = UNSET
        else:
            default_branch = self.default_branch

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "owner": owner,
                "name": name,
                "full_name": full_name,
                "clone_url_https": clone_url_https,
            }
        )
        if visibility is not UNSET:
            field_dict["visibility"] = visibility
        if clone_url_ssh is not UNSET:
            field_dict["clone_url_ssh"] = clone_url_ssh
        if default_branch is not UNSET:
            field_dict["default_branch"] = default_branch
        if description is not UNSET:
            field_dict["description"] = description

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        owner = d.pop("owner")

        name = d.pop("name")

        full_name = d.pop("full_name")

        clone_url_https = d.pop("clone_url_https")

        _visibility = d.pop("visibility", UNSET)
        visibility: GithubRepositoryVisibility | Unset
        if isinstance(_visibility, Unset):
            visibility = UNSET
        else:
            visibility = GithubRepositoryVisibility(_visibility)

        def _parse_clone_url_ssh(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        clone_url_ssh = _parse_clone_url_ssh(d.pop("clone_url_ssh", UNSET))

        def _parse_default_branch(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        default_branch = _parse_default_branch(d.pop("default_branch", UNSET))

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        github_repository = cls(
            owner=owner,
            name=name,
            full_name=full_name,
            clone_url_https=clone_url_https,
            visibility=visibility,
            clone_url_ssh=clone_url_ssh,
            default_branch=default_branch,
            description=description,
        )

        github_repository.additional_properties = d
        return github_repository

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
