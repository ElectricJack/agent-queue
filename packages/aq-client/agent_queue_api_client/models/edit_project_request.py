from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="EditProjectRequest")


@_attrs_define
class EditProjectRequest:
    """
    Attributes:
        project_id (str): Project ID
        name (None | str | Unset): New project name (optional)
        credit_weight (float | None | Unset): New scheduling weight (optional)
        max_concurrent_agents (int | None | Unset): New max concurrent agents (optional)
        budget_limit (int | None | Unset): Token budget limit (optional, null to clear)
        discord_channel_id (None | str | Unset): Discord channel ID to link (optional, null to unlink)
        default_profile_id (None | str | Unset): Default agent profile ID for tasks in this project (optional, null to
            clear)
        repo_default_branch (None | str | Unset): Default git branch for the project (e.g. main, dev, master)
    """

    project_id: str
    name: None | str | Unset = UNSET
    credit_weight: float | None | Unset = UNSET
    max_concurrent_agents: int | None | Unset = UNSET
    budget_limit: int | None | Unset = UNSET
    discord_channel_id: None | str | Unset = UNSET
    default_profile_id: None | str | Unset = UNSET
    repo_default_branch: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        credit_weight: float | None | Unset
        if isinstance(self.credit_weight, Unset):
            credit_weight = UNSET
        else:
            credit_weight = self.credit_weight

        max_concurrent_agents: int | None | Unset
        if isinstance(self.max_concurrent_agents, Unset):
            max_concurrent_agents = UNSET
        else:
            max_concurrent_agents = self.max_concurrent_agents

        budget_limit: int | None | Unset
        if isinstance(self.budget_limit, Unset):
            budget_limit = UNSET
        else:
            budget_limit = self.budget_limit

        discord_channel_id: None | str | Unset
        if isinstance(self.discord_channel_id, Unset):
            discord_channel_id = UNSET
        else:
            discord_channel_id = self.discord_channel_id

        default_profile_id: None | str | Unset
        if isinstance(self.default_profile_id, Unset):
            default_profile_id = UNSET
        else:
            default_profile_id = self.default_profile_id

        repo_default_branch: None | str | Unset
        if isinstance(self.repo_default_branch, Unset):
            repo_default_branch = UNSET
        else:
            repo_default_branch = self.repo_default_branch

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if credit_weight is not UNSET:
            field_dict["credit_weight"] = credit_weight
        if max_concurrent_agents is not UNSET:
            field_dict["max_concurrent_agents"] = max_concurrent_agents
        if budget_limit is not UNSET:
            field_dict["budget_limit"] = budget_limit
        if discord_channel_id is not UNSET:
            field_dict["discord_channel_id"] = discord_channel_id
        if default_profile_id is not UNSET:
            field_dict["default_profile_id"] = default_profile_id
        if repo_default_branch is not UNSET:
            field_dict["repo_default_branch"] = repo_default_branch

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        def _parse_credit_weight(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        credit_weight = _parse_credit_weight(d.pop("credit_weight", UNSET))

        def _parse_max_concurrent_agents(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_concurrent_agents = _parse_max_concurrent_agents(d.pop("max_concurrent_agents", UNSET))

        def _parse_budget_limit(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        budget_limit = _parse_budget_limit(d.pop("budget_limit", UNSET))

        def _parse_discord_channel_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        discord_channel_id = _parse_discord_channel_id(d.pop("discord_channel_id", UNSET))

        def _parse_default_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        default_profile_id = _parse_default_profile_id(d.pop("default_profile_id", UNSET))

        def _parse_repo_default_branch(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        repo_default_branch = _parse_repo_default_branch(d.pop("repo_default_branch", UNSET))

        edit_project_request = cls(
            project_id=project_id,
            name=name,
            credit_weight=credit_weight,
            max_concurrent_agents=max_concurrent_agents,
            budget_limit=budget_limit,
            discord_channel_id=discord_channel_id,
            default_profile_id=default_profile_id,
            repo_default_branch=repo_default_branch,
        )

        edit_project_request.additional_properties = d
        return edit_project_request

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
