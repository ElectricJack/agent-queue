from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WorkspaceOwnerTask")


@_attrs_define
class WorkspaceOwnerTask:
    """
    Attributes:
        id (str):
        found (bool):
        status (None | str | Unset):
        claim_epoch (int | None | Unset):
        claimed_by_session (None | str | Unset):
    """

    id: str
    found: bool
    status: None | str | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET
    claimed_by_session: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        found = self.found

        status: None | str | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        else:
            status = self.status

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        claimed_by_session: None | str | Unset
        if isinstance(self.claimed_by_session, Unset):
            claimed_by_session = UNSET
        else:
            claimed_by_session = self.claimed_by_session

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "found": found,
            }
        )
        if status is not UNSET:
            field_dict["status"] = status
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch
        if claimed_by_session is not UNSET:
            field_dict["claimed_by_session"] = claimed_by_session

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        found = d.pop("found")

        def _parse_status(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        status = _parse_status(d.pop("status", UNSET))

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        def _parse_claimed_by_session(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        claimed_by_session = _parse_claimed_by_session(d.pop("claimed_by_session", UNSET))

        workspace_owner_task = cls(
            id=id,
            found=found,
            status=status,
            claim_epoch=claim_epoch,
            claimed_by_session=claimed_by_session,
        )

        workspace_owner_task.additional_properties = d
        return workspace_owner_task

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
