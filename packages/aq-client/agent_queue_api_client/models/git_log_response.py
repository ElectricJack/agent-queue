from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GitLogResponse")


@_attrs_define
class GitLogResponse:
    """
    Attributes:
        project_id (str):
        branch (str | Unset):  Default: ''.
        log (str | Unset):  Default: ''.
    """

    project_id: str
    branch: str | Unset = ""
    log: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        branch = self.branch

        log = self.log

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if branch is not UNSET:
            field_dict["branch"] = branch
        if log is not UNSET:
            field_dict["log"] = log

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        branch = d.pop("branch", UNSET)

        log = d.pop("log", UNSET)

        git_log_response = cls(
            project_id=project_id,
            branch=branch,
            log=log,
        )

        git_log_response.additional_properties = d
        return git_log_response

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
