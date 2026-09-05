from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.github_repository import GithubRepository


T = TypeVar("T", bound="SearchGithubRepositoriesResponse")


@_attrs_define
class SearchGithubRepositoriesResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        repositories (list[GithubRepository] | Unset):
        next_cursor (None | str | Unset):
    """

    success: bool | Unset = True
    repositories: list[GithubRepository] | Unset = UNSET
    next_cursor: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        repositories: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.repositories, Unset):
            repositories = []
            for repositories_item_data in self.repositories:
                repositories_item = repositories_item_data.to_dict()
                repositories.append(repositories_item)

        next_cursor: None | str | Unset
        if isinstance(self.next_cursor, Unset):
            next_cursor = UNSET
        else:
            next_cursor = self.next_cursor

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if repositories is not UNSET:
            field_dict["repositories"] = repositories
        if next_cursor is not UNSET:
            field_dict["next_cursor"] = next_cursor

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.github_repository import GithubRepository

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _repositories = d.pop("repositories", UNSET)
        repositories: list[GithubRepository] | Unset = UNSET
        if _repositories is not UNSET:
            repositories = []
            for repositories_item_data in _repositories:
                repositories_item = GithubRepository.from_dict(repositories_item_data)

                repositories.append(repositories_item)

        def _parse_next_cursor(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor", UNSET))

        search_github_repositories_response = cls(
            success=success,
            repositories=repositories,
            next_cursor=next_cursor,
        )

        search_github_repositories_response.additional_properties = d
        return search_github_repositories_response

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
