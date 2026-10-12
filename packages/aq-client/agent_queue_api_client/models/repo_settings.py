from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="RepoSettings")


@_attrs_define
class RepoSettings:
    """
    Attributes:
        id (str):
        source_type (str | Unset):  Default: 'clone'.
        url (str | Unset):  Default: ''.
        default_branch (str | Unset):  Default: 'main'.
    """

    id: str
    source_type: str | Unset = "clone"
    url: str | Unset = ""
    default_branch: str | Unset = "main"

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        source_type = self.source_type

        url = self.url

        default_branch = self.default_branch

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "id": id,
            }
        )
        if source_type is not UNSET:
            field_dict["source_type"] = source_type
        if url is not UNSET:
            field_dict["url"] = url
        if default_branch is not UNSET:
            field_dict["default_branch"] = default_branch

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        source_type = d.pop("source_type", UNSET)

        url = d.pop("url", UNSET)

        default_branch = d.pop("default_branch", UNSET)

        repo_settings = cls(
            id=id,
            source_type=source_type,
            url=url,
            default_branch=default_branch,
        )

        return repo_settings
