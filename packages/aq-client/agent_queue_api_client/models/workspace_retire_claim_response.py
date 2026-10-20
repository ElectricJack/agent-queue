from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WorkspaceRetireClaimResponse")


@_attrs_define
class WorkspaceRetireClaimResponse:
    """
    Attributes:
        workspace_id (str):
        retired (bool | Unset):  Default: False.
        detail (None | str | Unset):
        holder_task_id (None | str | Unset):
        holder_claim_epoch (int | None | Unset):
        holder_session_id (None | str | Unset):
        reason (None | str | Unset):
    """

    workspace_id: str
    retired: bool | Unset = False
    detail: None | str | Unset = UNSET
    holder_task_id: None | str | Unset = UNSET
    holder_claim_epoch: int | None | Unset = UNSET
    holder_session_id: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        workspace_id = self.workspace_id

        retired = self.retired

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

        holder_claim_epoch: int | None | Unset
        if isinstance(self.holder_claim_epoch, Unset):
            holder_claim_epoch = UNSET
        else:
            holder_claim_epoch = self.holder_claim_epoch

        holder_session_id: None | str | Unset
        if isinstance(self.holder_session_id, Unset):
            holder_session_id = UNSET
        else:
            holder_session_id = self.holder_session_id

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
        if retired is not UNSET:
            field_dict["retired"] = retired
        if detail is not UNSET:
            field_dict["detail"] = detail
        if holder_task_id is not UNSET:
            field_dict["holder_task_id"] = holder_task_id
        if holder_claim_epoch is not UNSET:
            field_dict["holder_claim_epoch"] = holder_claim_epoch
        if holder_session_id is not UNSET:
            field_dict["holder_session_id"] = holder_session_id
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        workspace_id = d.pop("workspace_id")

        retired = d.pop("retired", UNSET)

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

        def _parse_holder_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        holder_claim_epoch = _parse_holder_claim_epoch(d.pop("holder_claim_epoch", UNSET))

        def _parse_holder_session_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        holder_session_id = _parse_holder_session_id(d.pop("holder_session_id", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        workspace_retire_claim_response = cls(
            workspace_id=workspace_id,
            retired=retired,
            detail=detail,
            holder_task_id=holder_task_id,
            holder_claim_epoch=holder_claim_epoch,
            holder_session_id=holder_session_id,
            reason=reason,
        )

        workspace_retire_claim_response.additional_properties = d
        return workspace_retire_claim_response

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
