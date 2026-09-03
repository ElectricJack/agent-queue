from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PrMergeRequest")


@_attrs_define
class PrMergeRequest:
    """
    Attributes:
        project_id (str): ID of the project whose workspace checkout will be used.
        pr_url (str): Full GitHub PR URL, e.g. ``https://github.com/org/repo/pull/42``.
        method (str | Unset): Merge strategy (default: squash). Default: 'squash'.
        force (bool | Unset): Merge even when ``integration.merge_ci_policy: required`` would refuse because the PR's
            checks are red, still running, or unreadable.  For a human who has looked at the failure and judged it unrelated
            — the override is recorded in the result and the log. Default: False.
    """

    project_id: str
    pr_url: str
    method: str | Unset = "squash"
    force: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        pr_url = self.pr_url

        method = self.method

        force = self.force

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "pr_url": pr_url,
            }
        )
        if method is not UNSET:
            field_dict["method"] = method
        if force is not UNSET:
            field_dict["force"] = force

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        pr_url = d.pop("pr_url")

        method = d.pop("method", UNSET)

        force = d.pop("force", UNSET)

        pr_merge_request = cls(
            project_id=project_id,
            pr_url=pr_url,
            method=method,
            force=force,
        )

        pr_merge_request.additional_properties = d
        return pr_merge_request

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
