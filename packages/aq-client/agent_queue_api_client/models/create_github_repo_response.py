from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreateGithubRepoResponse")


@_attrs_define
class CreateGithubRepoResponse:
    """
    Attributes:
        created (bool | Unset):  Default: False.
        repo_url (str | Unset):  Default: ''.
        name (str | Unset):  Default: ''.
    """

    created: bool | Unset = False
    repo_url: str | Unset = ""
    name: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        created = self.created

        repo_url = self.repo_url

        name = self.name

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if created is not UNSET:
            field_dict["created"] = created
        if repo_url is not UNSET:
            field_dict["repo_url"] = repo_url
        if name is not UNSET:
            field_dict["name"] = name

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        created = d.pop("created", UNSET)

        repo_url = d.pop("repo_url", UNSET)

        name = d.pop("name", UNSET)

        create_github_repo_response = cls(
            created=created,
            repo_url=repo_url,
            name=name,
        )

        create_github_repo_response.additional_properties = d
        return create_github_repo_response

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
