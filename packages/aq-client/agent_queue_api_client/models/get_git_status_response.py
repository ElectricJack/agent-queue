from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.get_git_status_response_repos_item import GetGitStatusResponseReposItem


T = TypeVar("T", bound="GetGitStatusResponse")


@_attrs_define
class GetGitStatusResponse:
    """
    Attributes:
        project_id (str):
        project_name (str | Unset):  Default: ''.
        repos (list[GetGitStatusResponseReposItem] | Unset):
    """

    project_id: str
    project_name: str | Unset = ""
    repos: list[GetGitStatusResponseReposItem] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        project_name = self.project_name

        repos: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.repos, Unset):
            repos = []
            for repos_item_data in self.repos:
                repos_item = repos_item_data.to_dict()
                repos.append(repos_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if project_name is not UNSET:
            field_dict["project_name"] = project_name
        if repos is not UNSET:
            field_dict["repos"] = repos

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.get_git_status_response_repos_item import GetGitStatusResponseReposItem

        d = dict(src_dict)
        project_id = d.pop("project_id")

        project_name = d.pop("project_name", UNSET)

        _repos = d.pop("repos", UNSET)
        repos: list[GetGitStatusResponseReposItem] | Unset = UNSET
        if _repos is not UNSET:
            repos = []
            for repos_item_data in _repos:
                repos_item = GetGitStatusResponseReposItem.from_dict(repos_item_data)

                repos.append(repos_item)

        get_git_status_response = cls(
            project_id=project_id,
            project_name=project_name,
            repos=repos,
        )

        get_git_status_response.additional_properties = d
        return get_git_status_response

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
