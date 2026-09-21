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
