from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="KnowledgeCiteRequest")


@_attrs_define
class KnowledgeCiteRequest:
    """
    Attributes:
        identity (str):
        revision_id (str):
        kind (str):
        idempotency_key (str):
        project_id (None | str | Unset):
        global_scope (bool | Unset):  Default: False.
        claim_epoch (int | None | Unset):
    """

    identity: str
    revision_id: str
    kind: str
    idempotency_key: str
    project_id: None | str | Unset = UNSET
    global_scope: bool | Unset = False
    claim_epoch: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        identity = self.identity

        revision_id = self.revision_id

        kind = self.kind

        idempotency_key = self.idempotency_key

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        global_scope = self.global_scope

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "identity": identity,
                "revision_id": revision_id,
                "kind": kind,
                "idempotency_key": idempotency_key,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if global_scope is not UNSET:
            field_dict["global_scope"] = global_scope
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        identity = d.pop("identity")

        revision_id = d.pop("revision_id")

        kind = d.pop("kind")

        idempotency_key = d.pop("idempotency_key")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        global_scope = d.pop("global_scope", UNSET)

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        knowledge_cite_request = cls(
            identity=identity,
            revision_id=revision_id,
            kind=kind,
            idempotency_key=idempotency_key,
            project_id=project_id,
            global_scope=global_scope,
            claim_epoch=claim_epoch,
        )

        knowledge_cite_request.additional_properties = d
        return knowledge_cite_request

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
