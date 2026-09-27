from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskDeliverResponse")


@_attrs_define
class TaskDeliverResponse:
    """``task_deliver``: a BLOCKED task's pushed branch merged into default by hand.

    ``method`` is ``fast_forward``, ``merge`` or ``already_delivered``;
    ``outcome`` is ``delivered`` / ``would_deliver`` on success, otherwise the
    refusal (``not_blocked``, ``pull_request_mode``, ``conflict``, ...).

        Attributes:
            success (bool):
            outcome (str | Unset):  Default: ''.
            task_id (None | str | Unset):
            repository_url (None | str | Unset):
            branch (None | str | Unset):
            default_branch (None | str | Unset):
            base_sha (None | str | Unset):
            head_sha (None | str | Unset):
            method (None | str | Unset):
            delivered_sha (None | str | Unset):
            conflict_files (list[str] | Unset):
            open_children (list[str] | Unset):
            error (None | str | Unset):
    """

    success: bool
    outcome: str | Unset = ""
    task_id: None | str | Unset = UNSET
    repository_url: None | str | Unset = UNSET
    branch: None | str | Unset = UNSET
    default_branch: None | str | Unset = UNSET
    base_sha: None | str | Unset = UNSET
    head_sha: None | str | Unset = UNSET
    method: None | str | Unset = UNSET
    delivered_sha: None | str | Unset = UNSET
    conflict_files: list[str] | Unset = UNSET
    open_children: list[str] | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        outcome = self.outcome

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        repository_url: None | str | Unset
        if isinstance(self.repository_url, Unset):
            repository_url = UNSET
        else:
            repository_url = self.repository_url

        branch: None | str | Unset
        if isinstance(self.branch, Unset):
            branch = UNSET
        else:
            branch = self.branch

        default_branch: None | str | Unset
        if isinstance(self.default_branch, Unset):
            default_branch = UNSET
        else:
            default_branch = self.default_branch

        base_sha: None | str | Unset
        if isinstance(self.base_sha, Unset):
            base_sha = UNSET
        else:
            base_sha = self.base_sha

        head_sha: None | str | Unset
        if isinstance(self.head_sha, Unset):
            head_sha = UNSET
        else:
            head_sha = self.head_sha

        method: None | str | Unset
        if isinstance(self.method, Unset):
            method = UNSET
        else:
            method = self.method

        delivered_sha: None | str | Unset
        if isinstance(self.delivered_sha, Unset):
            delivered_sha = UNSET
        else:
            delivered_sha = self.delivered_sha

        conflict_files: list[str] | Unset = UNSET
        if not isinstance(self.conflict_files, Unset):
            conflict_files = self.conflict_files

        open_children: list[str] | Unset = UNSET
        if not isinstance(self.open_children, Unset):
            open_children = self.open_children

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
            }
        )
        if outcome is not UNSET:
            field_dict["outcome"] = outcome
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if repository_url is not UNSET:
            field_dict["repository_url"] = repository_url
        if branch is not UNSET:
            field_dict["branch"] = branch
        if default_branch is not UNSET:
            field_dict["default_branch"] = default_branch
        if base_sha is not UNSET:
            field_dict["base_sha"] = base_sha
        if head_sha is not UNSET:
            field_dict["head_sha"] = head_sha
        if method is not UNSET:
            field_dict["method"] = method
        if delivered_sha is not UNSET:
            field_dict["delivered_sha"] = delivered_sha
        if conflict_files is not UNSET:
            field_dict["conflict_files"] = conflict_files
        if open_children is not UNSET:
            field_dict["open_children"] = open_children
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success")

        outcome = d.pop("outcome", UNSET)

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_repository_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        repository_url = _parse_repository_url(d.pop("repository_url", UNSET))

        def _parse_branch(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        branch = _parse_branch(d.pop("branch", UNSET))

        def _parse_default_branch(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        default_branch = _parse_default_branch(d.pop("default_branch", UNSET))

        def _parse_base_sha(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        base_sha = _parse_base_sha(d.pop("base_sha", UNSET))

        def _parse_head_sha(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        head_sha = _parse_head_sha(d.pop("head_sha", UNSET))

        def _parse_method(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        method = _parse_method(d.pop("method", UNSET))

        def _parse_delivered_sha(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        delivered_sha = _parse_delivered_sha(d.pop("delivered_sha", UNSET))

        conflict_files = cast(list[str], d.pop("conflict_files", UNSET))

        open_children = cast(list[str], d.pop("open_children", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        task_deliver_response = cls(
            success=success,
            outcome=outcome,
            task_id=task_id,
            repository_url=repository_url,
            branch=branch,
            default_branch=default_branch,
            base_sha=base_sha,
            head_sha=head_sha,
            method=method,
            delivered_sha=delivered_sha,
            conflict_files=conflict_files,
            open_children=open_children,
            error=error,
        )

        task_deliver_response.additional_properties = d
        return task_deliver_response

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
