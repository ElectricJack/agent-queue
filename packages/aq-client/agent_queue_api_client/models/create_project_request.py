from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreateProjectRequest")


@_attrs_define
class CreateProjectRequest:
    """
    Attributes:
        name (str): Project name
        credit_weight (float | Unset): Scheduling weight (default 1.0) Default: 1.0.
        max_concurrent_agents (int | Unset): Max agents working on this project simultaneously Default: 2.
        repo_url (None | str | Unset): Git repository URL for this project (optional)
        default_branch (str | Unset): Default branch name (default: main) Default: 'main'.
        auto_create_channels (bool | None | Unset): If true, auto-create dedicated Discord channels for this project
            after creation.  If false, skip channel creation.  When omitted, falls back to the global
            per_project_channels.auto_create config setting.
    """

    name: str
    credit_weight: float | Unset = 1.0
    max_concurrent_agents: int | Unset = 2
    repo_url: None | str | Unset = UNSET
    default_branch: str | Unset = "main"
    auto_create_channels: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        credit_weight = self.credit_weight

        max_concurrent_agents = self.max_concurrent_agents

        repo_url: None | str | Unset
        if isinstance(self.repo_url, Unset):
            repo_url = UNSET
        else:
            repo_url = self.repo_url

        default_branch = self.default_branch

        auto_create_channels: bool | None | Unset
        if isinstance(self.auto_create_channels, Unset):
            auto_create_channels = UNSET
        else:
            auto_create_channels = self.auto_create_channels

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
            }
        )
        if credit_weight is not UNSET:
            field_dict["credit_weight"] = credit_weight
        if max_concurrent_agents is not UNSET:
            field_dict["max_concurrent_agents"] = max_concurrent_agents
        if repo_url is not UNSET:
            field_dict["repo_url"] = repo_url
        if default_branch is not UNSET:
            field_dict["default_branch"] = default_branch
        if auto_create_channels is not UNSET:
            field_dict["auto_create_channels"] = auto_create_channels

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        credit_weight = d.pop("credit_weight", UNSET)

        max_concurrent_agents = d.pop("max_concurrent_agents", UNSET)

        def _parse_repo_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        repo_url = _parse_repo_url(d.pop("repo_url", UNSET))

        default_branch = d.pop("default_branch", UNSET)

        def _parse_auto_create_channels(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        auto_create_channels = _parse_auto_create_channels(d.pop("auto_create_channels", UNSET))

        create_project_request = cls(
            name=name,
            credit_weight=credit_weight,
            max_concurrent_agents=max_concurrent_agents,
            repo_url=repo_url,
            default_branch=default_branch,
            auto_create_channels=auto_create_channels,
        )

        create_project_request.additional_properties = d
        return create_project_request

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
