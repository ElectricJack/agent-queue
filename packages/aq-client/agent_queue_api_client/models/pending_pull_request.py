from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.pending_pull_request_state import PendingPullRequestState

T = TypeVar("T", bound="PendingPullRequest")


@_attrs_define
class PendingPullRequest:
    """
    Attributes:
        title (str):
        url (str):
        repository (str):
        project_id (str):
        project_name (str):
        task_id (str):
        state (PendingPullRequestState):
        opened_at (float | None):
    """

    title: str
    url: str
    repository: str
    project_id: str
    project_name: str
    task_id: str
    state: PendingPullRequestState
    opened_at: float | None
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        title = self.title

        url = self.url

        repository = self.repository

        project_id = self.project_id

        project_name = self.project_name

        task_id = self.task_id

        state = self.state.value

        opened_at: float | None
        opened_at = self.opened_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "title": title,
                "url": url,
                "repository": repository,
                "project_id": project_id,
                "project_name": project_name,
                "task_id": task_id,
                "state": state,
                "opened_at": opened_at,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        title = d.pop("title")

        url = d.pop("url")

        repository = d.pop("repository")

        project_id = d.pop("project_id")

        project_name = d.pop("project_name")

        task_id = d.pop("task_id")

        state = PendingPullRequestState(d.pop("state"))

        def _parse_opened_at(data: object) -> float | None:
            if data is None:
                return data
            return cast(float | None, data)

        opened_at = _parse_opened_at(d.pop("opened_at"))

        pending_pull_request = cls(
            title=title,
            url=url,
            repository=repository,
            project_id=project_id,
            project_name=project_name,
            task_id=task_id,
            state=state,
            opened_at=opened_at,
        )

        pending_pull_request.additional_properties = d
        return pending_pull_request

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
