from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GetProjectResponse")


@_attrs_define
class GetProjectResponse:
    """
    Attributes:
        id (str):
        name (str):
        status (str | Unset):  Default: ''.
        repo_url (str | Unset):  Default: ''.
        repo_default_branch (str | Unset):  Default: 'main'.
        workspace (None | str | Unset):
        credit_weight (float | Unset):  Default: 1.0.
        max_concurrent_agents (int | Unset):  Default: 1.
        total_tokens_used (int | Unset):  Default: 0.
        tokens_used_recent (int | Unset):  Default: 0.
        budget_limit (int | None | Unset):
        discord_channel_id (None | str | Unset):
        default_profile_id (None | str | Unset):
        assignment_playbook_id (None | str | Unset):
    """

    id: str
    name: str
    status: str | Unset = ""
    repo_url: str | Unset = ""
    repo_default_branch: str | Unset = "main"
    workspace: None | str | Unset = UNSET
    credit_weight: float | Unset = 1.0
    max_concurrent_agents: int | Unset = 1
    total_tokens_used: int | Unset = 0
    tokens_used_recent: int | Unset = 0
    budget_limit: int | None | Unset = UNSET
    discord_channel_id: None | str | Unset = UNSET
    default_profile_id: None | str | Unset = UNSET
    assignment_playbook_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        name = self.name

        status = self.status

        repo_url = self.repo_url

        repo_default_branch = self.repo_default_branch

        workspace: None | str | Unset
        if isinstance(self.workspace, Unset):
            workspace = UNSET
        else:
            workspace = self.workspace

        credit_weight = self.credit_weight

        max_concurrent_agents = self.max_concurrent_agents

        total_tokens_used = self.total_tokens_used

        tokens_used_recent = self.tokens_used_recent

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

        assignment_playbook_id: None | str | Unset
        if isinstance(self.assignment_playbook_id, Unset):
            assignment_playbook_id = UNSET
        else:
            assignment_playbook_id = self.assignment_playbook_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "name": name,
            }
        )
        if status is not UNSET:
            field_dict["status"] = status
        if repo_url is not UNSET:
            field_dict["repo_url"] = repo_url
        if repo_default_branch is not UNSET:
            field_dict["repo_default_branch"] = repo_default_branch
        if workspace is not UNSET:
            field_dict["workspace"] = workspace
        if credit_weight is not UNSET:
            field_dict["credit_weight"] = credit_weight
        if max_concurrent_agents is not UNSET:
            field_dict["max_concurrent_agents"] = max_concurrent_agents
        if total_tokens_used is not UNSET:
            field_dict["total_tokens_used"] = total_tokens_used
        if tokens_used_recent is not UNSET:
            field_dict["tokens_used_recent"] = tokens_used_recent
        if budget_limit is not UNSET:
            field_dict["budget_limit"] = budget_limit
        if discord_channel_id is not UNSET:
            field_dict["discord_channel_id"] = discord_channel_id
        if default_profile_id is not UNSET:
            field_dict["default_profile_id"] = default_profile_id
        if assignment_playbook_id is not UNSET:
            field_dict["assignment_playbook_id"] = assignment_playbook_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        status = d.pop("status", UNSET)

        repo_url = d.pop("repo_url", UNSET)

        repo_default_branch = d.pop("repo_default_branch", UNSET)

        def _parse_workspace(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        workspace = _parse_workspace(d.pop("workspace", UNSET))

        credit_weight = d.pop("credit_weight", UNSET)

        max_concurrent_agents = d.pop("max_concurrent_agents", UNSET)

        total_tokens_used = d.pop("total_tokens_used", UNSET)

        tokens_used_recent = d.pop("tokens_used_recent", UNSET)

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

        def _parse_assignment_playbook_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        assignment_playbook_id = _parse_assignment_playbook_id(d.pop("assignment_playbook_id", UNSET))

        get_project_response = cls(
            id=id,
            name=name,
            status=status,
            repo_url=repo_url,
            repo_default_branch=repo_default_branch,
            workspace=workspace,
            credit_weight=credit_weight,
            max_concurrent_agents=max_concurrent_agents,
            total_tokens_used=total_tokens_used,
            tokens_used_recent=tokens_used_recent,
            budget_limit=budget_limit,
            discord_channel_id=discord_channel_id,
            default_profile_id=default_profile_id,
            assignment_playbook_id=assignment_playbook_id,
        )

        get_project_response.additional_properties = d
        return get_project_response

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
