from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.onboard_project_request_github_repository_type_0 import OnboardProjectRequestGithubRepositoryType0


T = TypeVar("T", bound="OnboardProjectRequest")


@_attrs_define
class OnboardProjectRequest:
    """
    Attributes:
        request_id (str): Idempotency key; replaying it returns the prior result
        source_mode (str): link an existing repo, init a new one, or clone from GitHub
        root_id (str): Configured project root id
        relative_path (str): Root-relative destination: the existing repository (link) or the directory to create (init,
            github_clone)
        project_name (str): Display name
        project_id (str): URL-safe project id (slug)
        default_branch (None | str | Unset): Default branch; detected for link/github_clone and `main` for init when
            omitted
        create_readme (bool | None | Unset): init only: create README.md and an initial commit (default true)
        create_github (bool | None | Unset): init only: also create a GitHub repository (default false)
        github_owner (None | str | Unset): init with create_github: owner to create the repository under
        github_repo (None | str | Unset): init with create_github: repository name (default: destination directory name)
        github_visibility (None | str | Unset): init with create_github: visibility (default private)
        github_repository (None | OnboardProjectRequestGithubRepositoryType0 | Unset): github_clone only: {owner, name}
            selected through search_github_repositories (exactly one of github_repository / github_url)
        github_url (None | str | Unset): github_clone only: pasted GitHub HTTPS/SSH URL or owner/name shorthand (exactly
            one of github_repository / github_url)
    """

    request_id: str
    source_mode: str
    root_id: str
    relative_path: str
    project_name: str
    project_id: str
    default_branch: None | str | Unset = UNSET
    create_readme: bool | None | Unset = UNSET
    create_github: bool | None | Unset = UNSET
    github_owner: None | str | Unset = UNSET
    github_repo: None | str | Unset = UNSET
    github_visibility: None | str | Unset = UNSET
    github_repository: None | OnboardProjectRequestGithubRepositoryType0 | Unset = UNSET
    github_url: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.onboard_project_request_github_repository_type_0 import OnboardProjectRequestGithubRepositoryType0

        request_id = self.request_id

        source_mode = self.source_mode

        root_id = self.root_id

        relative_path = self.relative_path

        project_name = self.project_name

        project_id = self.project_id

        default_branch: None | str | Unset
        if isinstance(self.default_branch, Unset):
            default_branch = UNSET
        else:
            default_branch = self.default_branch

        create_readme: bool | None | Unset
        if isinstance(self.create_readme, Unset):
            create_readme = UNSET
        else:
            create_readme = self.create_readme

        create_github: bool | None | Unset
        if isinstance(self.create_github, Unset):
            create_github = UNSET
        else:
            create_github = self.create_github

        github_owner: None | str | Unset
        if isinstance(self.github_owner, Unset):
            github_owner = UNSET
        else:
            github_owner = self.github_owner

        github_repo: None | str | Unset
        if isinstance(self.github_repo, Unset):
            github_repo = UNSET
        else:
            github_repo = self.github_repo

        github_visibility: None | str | Unset
        if isinstance(self.github_visibility, Unset):
            github_visibility = UNSET
        else:
            github_visibility = self.github_visibility

        github_repository: dict[str, Any] | None | Unset
        if isinstance(self.github_repository, Unset):
            github_repository = UNSET
        elif isinstance(self.github_repository, OnboardProjectRequestGithubRepositoryType0):
            github_repository = self.github_repository.to_dict()
        else:
            github_repository = self.github_repository

        github_url: None | str | Unset
        if isinstance(self.github_url, Unset):
            github_url = UNSET
        else:
            github_url = self.github_url

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "request_id": request_id,
                "source_mode": source_mode,
                "root_id": root_id,
                "relative_path": relative_path,
                "project_name": project_name,
                "project_id": project_id,
            }
        )
        if default_branch is not UNSET:
            field_dict["default_branch"] = default_branch
        if create_readme is not UNSET:
            field_dict["create_readme"] = create_readme
        if create_github is not UNSET:
            field_dict["create_github"] = create_github
        if github_owner is not UNSET:
            field_dict["github_owner"] = github_owner
        if github_repo is not UNSET:
            field_dict["github_repo"] = github_repo
        if github_visibility is not UNSET:
            field_dict["github_visibility"] = github_visibility
        if github_repository is not UNSET:
            field_dict["github_repository"] = github_repository
        if github_url is not UNSET:
            field_dict["github_url"] = github_url

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.onboard_project_request_github_repository_type_0 import OnboardProjectRequestGithubRepositoryType0

        d = dict(src_dict)
        request_id = d.pop("request_id")

        source_mode = d.pop("source_mode")

        root_id = d.pop("root_id")

        relative_path = d.pop("relative_path")

        project_name = d.pop("project_name")

        project_id = d.pop("project_id")

        def _parse_default_branch(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        default_branch = _parse_default_branch(d.pop("default_branch", UNSET))

        def _parse_create_readme(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        create_readme = _parse_create_readme(d.pop("create_readme", UNSET))

        def _parse_create_github(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        create_github = _parse_create_github(d.pop("create_github", UNSET))

        def _parse_github_owner(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        github_owner = _parse_github_owner(d.pop("github_owner", UNSET))

        def _parse_github_repo(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        github_repo = _parse_github_repo(d.pop("github_repo", UNSET))

        def _parse_github_visibility(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        github_visibility = _parse_github_visibility(d.pop("github_visibility", UNSET))

        def _parse_github_repository(data: object) -> None | OnboardProjectRequestGithubRepositoryType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                github_repository_type_0 = OnboardProjectRequestGithubRepositoryType0.from_dict(data)

                return github_repository_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OnboardProjectRequestGithubRepositoryType0 | Unset, data)

        github_repository = _parse_github_repository(d.pop("github_repository", UNSET))

        def _parse_github_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        github_url = _parse_github_url(d.pop("github_url", UNSET))

        onboard_project_request = cls(
            request_id=request_id,
            source_mode=source_mode,
            root_id=root_id,
            relative_path=relative_path,
            project_name=project_name,
            project_id=project_id,
            default_branch=default_branch,
            create_readme=create_readme,
            create_github=create_github,
            github_owner=github_owner,
            github_repo=github_repo,
            github_visibility=github_visibility,
            github_repository=github_repository,
            github_url=github_url,
        )

        onboard_project_request.additional_properties = d
        return onboard_project_request

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
