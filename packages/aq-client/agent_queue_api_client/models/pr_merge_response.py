from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PrMergeResponse")


@_attrs_define
class PrMergeResponse:
    """Response model for the ``pr_merge`` command.

    Attributes:
        success (bool | Unset):  Default: False.
        pr_url (str | Unset):  Default: ''.
        sha (None | str | Unset):
        error (None | str | Unset):
    """

    success: bool | Unset = False
    pr_url: str | Unset = ""
    sha: None | str | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        pr_url = self.pr_url

        sha: None | str | Unset
        if isinstance(self.sha, Unset):
            sha = UNSET
        else:
            sha = self.sha

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if pr_url is not UNSET:
            field_dict["pr_url"] = pr_url
        if sha is not UNSET:
            field_dict["sha"] = sha
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success", UNSET)

        pr_url = d.pop("pr_url", UNSET)

        def _parse_sha(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        sha = _parse_sha(d.pop("sha", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        pr_merge_response = cls(
            success=success,
            pr_url=pr_url,
            sha=sha,
            error=error,
        )

        pr_merge_response.additional_properties = d
        return pr_merge_response

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
