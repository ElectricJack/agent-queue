from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="KnowledgeCreateTaskRequest")


@_attrs_define
class KnowledgeCreateTaskRequest:
    """
    Attributes:
        project_id (str):
        identity (str):
        revision_id (str):
        title (str):
        description (str):
        idempotency_key (str):
        task_type (None | str | Unset):
        priority (int | Unset):  Default: 100.
        parent_id (None | str | Unset):
        root (bool | Unset):  Default: False.
        reason (None | str | Unset):
        claim_epoch (int | None | Unset):
    """

    project_id: str
    identity: str
    revision_id: str
    title: str
    description: str
    idempotency_key: str
    task_type: None | str | Unset = UNSET
    priority: int | Unset = 100
    parent_id: None | str | Unset = UNSET
    root: bool | Unset = False
    reason: None | str | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        identity = self.identity

        revision_id = self.revision_id

        title = self.title

        description = self.description

        idempotency_key = self.idempotency_key

        task_type: None | str | Unset
        if isinstance(self.task_type, Unset):
            task_type = UNSET
        else:
            task_type = self.task_type

        priority = self.priority

        parent_id: None | str | Unset
        if isinstance(self.parent_id, Unset):
            parent_id = UNSET
        else:
            parent_id = self.parent_id

        root = self.root

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "identity": identity,
                "revision_id": revision_id,
                "title": title,
                "description": description,
                "idempotency_key": idempotency_key,
            }
        )
        if task_type is not UNSET:
            field_dict["task_type"] = task_type
        if priority is not UNSET:
            field_dict["priority"] = priority
        if parent_id is not UNSET:
            field_dict["parent_id"] = parent_id
        if root is not UNSET:
            field_dict["root"] = root
        if reason is not UNSET:
            field_dict["reason"] = reason
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        identity = d.pop("identity")

        revision_id = d.pop("revision_id")

        title = d.pop("title")

        description = d.pop("description")

        idempotency_key = d.pop("idempotency_key")

        def _parse_task_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_type = _parse_task_type(d.pop("task_type", UNSET))

        priority = d.pop("priority", UNSET)

        def _parse_parent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_id = _parse_parent_id(d.pop("parent_id", UNSET))

        root = d.pop("root", UNSET)

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        knowledge_create_task_request = cls(
            project_id=project_id,
            identity=identity,
            revision_id=revision_id,
            title=title,
            description=description,
            idempotency_key=idempotency_key,
            task_type=task_type,
            priority=priority,
            parent_id=parent_id,
            root=root,
            reason=reason,
            claim_epoch=claim_epoch,
        )

        knowledge_create_task_request.additional_properties = d
        return knowledge_create_task_request

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
