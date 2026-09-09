from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="DeletedBranch")


@_attrs_define
class DeletedBranch:
    """One branch a ``branch_discard_required`` refusal is asking about.

    Attributes:
        task_id (str):
        branch (str):
        base_sha (str):
    """

    task_id: str
    branch: str
    base_sha: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        branch = self.branch

        base_sha = self.base_sha

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "branch": branch,
                "base_sha": base_sha,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        branch = d.pop("branch")

        base_sha = d.pop("base_sha")

        deleted_branch = cls(
            task_id=task_id,
            branch=branch,
            base_sha=base_sha,
        )

        deleted_branch.additional_properties = d
        return deleted_branch

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
