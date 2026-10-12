from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.project_settings_integration_preferences import ProjectSettingsIntegrationPreferences
    from ..models.repo_settings import RepoSettings


T = TypeVar("T", bound="ProjectSettings")


@_attrs_define
class ProjectSettings:
    """
    Attributes:
        name (str):
        credit_weight (float | Unset):  Default: 1.0.
        max_concurrent_agents (int | Unset):  Default: 2.
        status (str | Unset):  Default: 'ACTIVE'.
        budget_limit (int | None | Unset):
        repo_url (str | Unset):  Default: ''.
        repo_default_branch (str | Unset):  Default: 'main'.
        assignment_playbook_id (str | Unset):  Default: 'default-assignment-routing'.
        integration_mode (None | str | Unset):
        review_delegate_to (None | str | Unset):
        preferred_provider (None | str | Unset):
        repos (list[RepoSettings] | Unset):
        integration_preferences (ProjectSettingsIntegrationPreferences | Unset):
    """

    name: str
    credit_weight: float | Unset = 1.0
    max_concurrent_agents: int | Unset = 2
    status: str | Unset = "ACTIVE"
    budget_limit: int | None | Unset = UNSET
    repo_url: str | Unset = ""
    repo_default_branch: str | Unset = "main"
    assignment_playbook_id: str | Unset = "default-assignment-routing"
    integration_mode: None | str | Unset = UNSET
    review_delegate_to: None | str | Unset = UNSET
    preferred_provider: None | str | Unset = UNSET
    repos: list[RepoSettings] | Unset = UNSET
    integration_preferences: ProjectSettingsIntegrationPreferences | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        credit_weight = self.credit_weight

        max_concurrent_agents = self.max_concurrent_agents

        status = self.status

        budget_limit: int | None | Unset
        if isinstance(self.budget_limit, Unset):
            budget_limit = UNSET
        else:
            budget_limit = self.budget_limit

        repo_url = self.repo_url

        repo_default_branch = self.repo_default_branch

        assignment_playbook_id = self.assignment_playbook_id

        integration_mode: None | str | Unset
        if isinstance(self.integration_mode, Unset):
            integration_mode = UNSET
        else:
            integration_mode = self.integration_mode

        review_delegate_to: None | str | Unset
        if isinstance(self.review_delegate_to, Unset):
            review_delegate_to = UNSET
        else:
            review_delegate_to = self.review_delegate_to

        preferred_provider: None | str | Unset
        if isinstance(self.preferred_provider, Unset):
            preferred_provider = UNSET
        else:
            preferred_provider = self.preferred_provider

        repos: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.repos, Unset):
            repos = []
            for repos_item_data in self.repos:
                repos_item = repos_item_data.to_dict()
                repos.append(repos_item)

        integration_preferences: dict[str, Any] | Unset = UNSET
        if not isinstance(self.integration_preferences, Unset):
            integration_preferences = self.integration_preferences.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "name": name,
            }
        )
        if credit_weight is not UNSET:
            field_dict["credit_weight"] = credit_weight
        if max_concurrent_agents is not UNSET:
            field_dict["max_concurrent_agents"] = max_concurrent_agents
        if status is not UNSET:
            field_dict["status"] = status
        if budget_limit is not UNSET:
            field_dict["budget_limit"] = budget_limit
        if repo_url is not UNSET:
            field_dict["repo_url"] = repo_url
        if repo_default_branch is not UNSET:
            field_dict["repo_default_branch"] = repo_default_branch
        if assignment_playbook_id is not UNSET:
            field_dict["assignment_playbook_id"] = assignment_playbook_id
        if integration_mode is not UNSET:
            field_dict["integration_mode"] = integration_mode
        if review_delegate_to is not UNSET:
            field_dict["review_delegate_to"] = review_delegate_to
        if preferred_provider is not UNSET:
            field_dict["preferred_provider"] = preferred_provider
        if repos is not UNSET:
            field_dict["repos"] = repos
        if integration_preferences is not UNSET:
            field_dict["integration_preferences"] = integration_preferences

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.project_settings_integration_preferences import ProjectSettingsIntegrationPreferences
        from ..models.repo_settings import RepoSettings

        d = dict(src_dict)
        name = d.pop("name")

        credit_weight = d.pop("credit_weight", UNSET)

        max_concurrent_agents = d.pop("max_concurrent_agents", UNSET)

        status = d.pop("status", UNSET)

        def _parse_budget_limit(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        budget_limit = _parse_budget_limit(d.pop("budget_limit", UNSET))

        repo_url = d.pop("repo_url", UNSET)

        repo_default_branch = d.pop("repo_default_branch", UNSET)

        assignment_playbook_id = d.pop("assignment_playbook_id", UNSET)

        def _parse_integration_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        integration_mode = _parse_integration_mode(d.pop("integration_mode", UNSET))

        def _parse_review_delegate_to(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        review_delegate_to = _parse_review_delegate_to(d.pop("review_delegate_to", UNSET))

        def _parse_preferred_provider(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        preferred_provider = _parse_preferred_provider(d.pop("preferred_provider", UNSET))

        _repos = d.pop("repos", UNSET)
        repos: list[RepoSettings] | Unset = UNSET
        if _repos is not UNSET:
            repos = []
            for repos_item_data in _repos:
                repos_item = RepoSettings.from_dict(repos_item_data)

                repos.append(repos_item)

        _integration_preferences = d.pop("integration_preferences", UNSET)
        integration_preferences: ProjectSettingsIntegrationPreferences | Unset
        if isinstance(_integration_preferences, Unset):
            integration_preferences = UNSET
        else:
            integration_preferences = ProjectSettingsIntegrationPreferences.from_dict(_integration_preferences)

        project_settings = cls(
            name=name,
            credit_weight=credit_weight,
            max_concurrent_agents=max_concurrent_agents,
            status=status,
            budget_limit=budget_limit,
            repo_url=repo_url,
            repo_default_branch=repo_default_branch,
            assignment_playbook_id=assignment_playbook_id,
            integration_mode=integration_mode,
            review_delegate_to=review_delegate_to,
            preferred_provider=preferred_provider,
            repos=repos,
            integration_preferences=integration_preferences,
        )

        return project_settings
