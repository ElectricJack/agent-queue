from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreateGithubRepoRequest")


@_attrs_define
class CreateGithubRepoRequest:
    """
    Attributes:
        name (str): Repository name
        private (bool | Unset): Create private repo (default true) Default: True.
        org (None | str | Unset): GitHub org — omit for personal repo
        description (None | str | Unset): Optional repo description
    """

    name: str
    private: bool | Unset = True
    org: None | str | Unset = UNSET
    description: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        private = self.private

        org: None | str | Unset
        if isinstance(self.org, Unset):
            org = UNSET
        else:
            org = self.org

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
            }
        )
        if private is not UNSET:
            field_dict["private"] = private
        if org is not UNSET:
            field_dict["org"] = org
        if description is not UNSET:
            field_dict["description"] = description

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        private = d.pop("private", UNSET)

        def _parse_org(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        org = _parse_org(d.pop("org", UNSET))

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        create_github_repo_request = cls(
            name=name,
            private=private,
            org=org,
            description=description,
        )

        create_github_repo_request.additional_properties = d
        return create_github_repo_request

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
