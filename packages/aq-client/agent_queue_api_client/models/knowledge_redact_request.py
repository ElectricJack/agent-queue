from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="KnowledgeRedactRequest")


@_attrs_define
class KnowledgeRedactRequest:
    """
    Attributes:
        identity (str):
        idempotency_key (str):
        reason_code (str):
        project_id (None | str | Unset):
        global_scope (bool | Unset):  Default: False.
        if_revision (None | str | Unset):
        revision_id (None | str | Unset):
        dry_run (bool | Unset):  Default: True.
    """

    identity: str
    idempotency_key: str
    reason_code: str
    project_id: None | str | Unset = UNSET
    global_scope: bool | Unset = False
    if_revision: None | str | Unset = UNSET
    revision_id: None | str | Unset = UNSET
    dry_run: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        identity = self.identity

        idempotency_key = self.idempotency_key

        reason_code = self.reason_code

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        global_scope = self.global_scope

        if_revision: None | str | Unset
        if isinstance(self.if_revision, Unset):
            if_revision = UNSET
        else:
            if_revision = self.if_revision

        revision_id: None | str | Unset
        if isinstance(self.revision_id, Unset):
            revision_id = UNSET
        else:
            revision_id = self.revision_id

        dry_run = self.dry_run

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "identity": identity,
                "idempotency_key": idempotency_key,
                "reason_code": reason_code,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if global_scope is not UNSET:
            field_dict["global_scope"] = global_scope
        if if_revision is not UNSET:
            field_dict["if_revision"] = if_revision
        if revision_id is not UNSET:
            field_dict["revision_id"] = revision_id
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        identity = d.pop("identity")

        idempotency_key = d.pop("idempotency_key")

        reason_code = d.pop("reason_code")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        global_scope = d.pop("global_scope", UNSET)

        def _parse_if_revision(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        if_revision = _parse_if_revision(d.pop("if_revision", UNSET))

        def _parse_revision_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        revision_id = _parse_revision_id(d.pop("revision_id", UNSET))

        dry_run = d.pop("dry_run", UNSET)

        knowledge_redact_request = cls(
            identity=identity,
            idempotency_key=idempotency_key,
            reason_code=reason_code,
            project_id=project_id,
            global_scope=global_scope,
            if_revision=if_revision,
            revision_id=revision_id,
            dry_run=dry_run,
        )

        knowledge_redact_request.additional_properties = d
        return knowledge_redact_request

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
