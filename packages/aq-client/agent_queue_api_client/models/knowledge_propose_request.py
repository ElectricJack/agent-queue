from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.knowledge_propose_request_snapshot import KnowledgeProposeRequestSnapshot


T = TypeVar("T", bound="KnowledgeProposeRequest")


@_attrs_define
class KnowledgeProposeRequest:
    """
    Attributes:
        snapshot (KnowledgeProposeRequestSnapshot):
        idempotency_key (str):
        project_id (None | str | Unset):
        global_scope (bool | Unset):  Default: False.
        identity (None | str | Unset):
        if_revision (None | str | Unset):
        link_operations (list[Any] | None | Unset):
        claim_epoch (int | None | Unset):
    """

    snapshot: KnowledgeProposeRequestSnapshot
    idempotency_key: str
    project_id: None | str | Unset = UNSET
    global_scope: bool | Unset = False
    identity: None | str | Unset = UNSET
    if_revision: None | str | Unset = UNSET
    link_operations: list[Any] | None | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        snapshot = self.snapshot.to_dict()

        idempotency_key = self.idempotency_key

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        global_scope = self.global_scope

        identity: None | str | Unset
        if isinstance(self.identity, Unset):
            identity = UNSET
        else:
            identity = self.identity

        if_revision: None | str | Unset
        if isinstance(self.if_revision, Unset):
            if_revision = UNSET
        else:
            if_revision = self.if_revision

        link_operations: list[Any] | None | Unset
        if isinstance(self.link_operations, Unset):
            link_operations = UNSET
        elif isinstance(self.link_operations, list):
            link_operations = self.link_operations

        else:
            link_operations = self.link_operations

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "snapshot": snapshot,
                "idempotency_key": idempotency_key,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if global_scope is not UNSET:
            field_dict["global_scope"] = global_scope
        if identity is not UNSET:
            field_dict["identity"] = identity
        if if_revision is not UNSET:
            field_dict["if_revision"] = if_revision
        if link_operations is not UNSET:
            field_dict["link_operations"] = link_operations
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.knowledge_propose_request_snapshot import KnowledgeProposeRequestSnapshot

        d = dict(src_dict)
        snapshot = KnowledgeProposeRequestSnapshot.from_dict(d.pop("snapshot"))

        idempotency_key = d.pop("idempotency_key")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        global_scope = d.pop("global_scope", UNSET)

        def _parse_identity(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        identity = _parse_identity(d.pop("identity", UNSET))

        def _parse_if_revision(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        if_revision = _parse_if_revision(d.pop("if_revision", UNSET))

        def _parse_link_operations(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                link_operations_type_0 = cast(list[Any], data)

                return link_operations_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        link_operations = _parse_link_operations(d.pop("link_operations", UNSET))

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        knowledge_propose_request = cls(
            snapshot=snapshot,
            idempotency_key=idempotency_key,
            project_id=project_id,
            global_scope=global_scope,
            identity=identity,
            if_revision=if_revision,
            link_operations=link_operations,
            claim_epoch=claim_epoch,
        )

        knowledge_propose_request.additional_properties = d
        return knowledge_propose_request

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
