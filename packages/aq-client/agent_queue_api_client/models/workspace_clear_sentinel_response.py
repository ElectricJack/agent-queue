from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WorkspaceClearSentinelResponse")


@_attrs_define
class WorkspaceClearSentinelResponse:
    """
    Attributes:
        workspace_id (str):
        cleared (bool | Unset):  Default: False.
        detail (None | str | Unset):
        holder_task_id (None | str | Unset):
        branch (None | str | Unset):
        reason (None | str | Unset):
    """

    workspace_id: str
    cleared: bool | Unset = False
    detail: None | str | Unset = UNSET
    holder_task_id: None | str | Unset = UNSET
    branch: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        workspace_id = self.workspace_id

        cleared = self.cleared

        detail: None | str | Unset
        if isinstance(self.detail, Unset):
            detail = UNSET
        else:
            detail = self.detail

        holder_task_id: None | str | Unset
        if isinstance(self.holder_task_id, Unset):
            holder_task_id = UNSET
        else:
            holder_task_id = self.holder_task_id

        branch: None | str | Unset
        if isinstance(self.branch, Unset):
            branch = UNSET
        else:
            branch = self.branch

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "workspace_id": workspace_id,
            }
        )
        if cleared is not UNSET:
            field_dict["cleared"] = cleared
        if detail is not UNSET:
            field_dict["detail"] = detail
        if holder_task_id is not UNSET:
            field_dict["holder_task_id"] = holder_task_id
        if branch is not UNSET:
            field_dict["branch"] = branch
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        workspace_id = d.pop("workspace_id")

        cleared = d.pop("cleared", UNSET)

        def _parse_detail(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        detail = _parse_detail(d.pop("detail", UNSET))

        def _parse_holder_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        holder_task_id = _parse_holder_task_id(d.pop("holder_task_id", UNSET))

        def _parse_branch(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        branch = _parse_branch(d.pop("branch", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        workspace_clear_sentinel_response = cls(
            workspace_id=workspace_id,
            cleared=cleared,
            detail=detail,
            holder_task_id=holder_task_id,
            branch=branch,
            reason=reason,
        )

        workspace_clear_sentinel_response.additional_properties = d
        return workspace_clear_sentinel_response

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
