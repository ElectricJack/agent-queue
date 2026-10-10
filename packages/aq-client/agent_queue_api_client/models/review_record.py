from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReviewRecord")


@_attrs_define
class ReviewRecord:
    """
    Attributes:
        id (str):
        project_id (str):
        title (str):
        kind (str):
        state (str):
        current_revision (int):
        vault_path (str):
        author_task_id (None | str | Unset):
        gate_id (None | str | Unset):
        decider (str | Unset):  Default: 'user'.
        withdrawn_by (None | str | Unset):
        withdrawn_at (float | None | Unset):
        withdrawn_via (None | str | Unset):
        withdrawal_reason (None | str | Unset):
    """

    id: str
    project_id: str
    title: str
    kind: str
    state: str
    current_revision: int
    vault_path: str
    author_task_id: None | str | Unset = UNSET
    gate_id: None | str | Unset = UNSET
    decider: str | Unset = "user"
    withdrawn_by: None | str | Unset = UNSET
    withdrawn_at: float | None | Unset = UNSET
    withdrawn_via: None | str | Unset = UNSET
    withdrawal_reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        project_id = self.project_id

        title = self.title

        kind = self.kind

        state = self.state

        current_revision = self.current_revision

        vault_path = self.vault_path

        author_task_id: None | str | Unset
        if isinstance(self.author_task_id, Unset):
            author_task_id = UNSET
        else:
            author_task_id = self.author_task_id

        gate_id: None | str | Unset
        if isinstance(self.gate_id, Unset):
            gate_id = UNSET
        else:
            gate_id = self.gate_id

        decider = self.decider

        withdrawn_by: None | str | Unset
        if isinstance(self.withdrawn_by, Unset):
            withdrawn_by = UNSET
        else:
            withdrawn_by = self.withdrawn_by

        withdrawn_at: float | None | Unset
        if isinstance(self.withdrawn_at, Unset):
            withdrawn_at = UNSET
        else:
            withdrawn_at = self.withdrawn_at

        withdrawn_via: None | str | Unset
        if isinstance(self.withdrawn_via, Unset):
            withdrawn_via = UNSET
        else:
            withdrawn_via = self.withdrawn_via

        withdrawal_reason: None | str | Unset
        if isinstance(self.withdrawal_reason, Unset):
            withdrawal_reason = UNSET
        else:
            withdrawal_reason = self.withdrawal_reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "project_id": project_id,
                "title": title,
                "kind": kind,
                "state": state,
                "current_revision": current_revision,
                "vault_path": vault_path,
            }
        )
        if author_task_id is not UNSET:
            field_dict["author_task_id"] = author_task_id
        if gate_id is not UNSET:
            field_dict["gate_id"] = gate_id
        if decider is not UNSET:
            field_dict["decider"] = decider
        if withdrawn_by is not UNSET:
            field_dict["withdrawn_by"] = withdrawn_by
        if withdrawn_at is not UNSET:
            field_dict["withdrawn_at"] = withdrawn_at
        if withdrawn_via is not UNSET:
            field_dict["withdrawn_via"] = withdrawn_via
        if withdrawal_reason is not UNSET:
            field_dict["withdrawal_reason"] = withdrawal_reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        project_id = d.pop("project_id")

        title = d.pop("title")

        kind = d.pop("kind")

        state = d.pop("state")

        current_revision = d.pop("current_revision")

        vault_path = d.pop("vault_path")

        def _parse_author_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        author_task_id = _parse_author_task_id(d.pop("author_task_id", UNSET))

        def _parse_gate_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        gate_id = _parse_gate_id(d.pop("gate_id", UNSET))

        decider = d.pop("decider", UNSET)

        def _parse_withdrawn_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        withdrawn_by = _parse_withdrawn_by(d.pop("withdrawn_by", UNSET))

        def _parse_withdrawn_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        withdrawn_at = _parse_withdrawn_at(d.pop("withdrawn_at", UNSET))

        def _parse_withdrawn_via(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        withdrawn_via = _parse_withdrawn_via(d.pop("withdrawn_via", UNSET))

        def _parse_withdrawal_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        withdrawal_reason = _parse_withdrawal_reason(d.pop("withdrawal_reason", UNSET))

        review_record = cls(
            id=id,
            project_id=project_id,
            title=title,
            kind=kind,
            state=state,
            current_revision=current_revision,
            vault_path=vault_path,
            author_task_id=author_task_id,
            gate_id=gate_id,
            decider=decider,
            withdrawn_by=withdrawn_by,
            withdrawn_at=withdrawn_at,
            withdrawn_via=withdrawn_via,
            withdrawal_reason=withdrawal_reason,
        )

        review_record.additional_properties = d
        return review_record

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
