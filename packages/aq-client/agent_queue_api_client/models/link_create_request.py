from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="LinkCreateRequest")


@_attrs_define
class LinkCreateRequest:
    """
    Attributes:
        identity (str):
        operations (list[Any]):
        idempotency_key (str):
        project_id (None | str | Unset):
        if_revision (None | str | Unset):
        if_link_token (None | str | Unset):
        claim_epoch (int | None | Unset):
        global_scope (bool | Unset):  Default: False.
    """

    identity: str
    operations: list[Any]
    idempotency_key: str
    project_id: None | str | Unset = UNSET
    if_revision: None | str | Unset = UNSET
    if_link_token: None | str | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET
    global_scope: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        identity = self.identity

        operations = self.operations

        idempotency_key = self.idempotency_key

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        if_revision: None | str | Unset
        if isinstance(self.if_revision, Unset):
            if_revision = UNSET
        else:
            if_revision = self.if_revision

        if_link_token: None | str | Unset
        if isinstance(self.if_link_token, Unset):
            if_link_token = UNSET
        else:
            if_link_token = self.if_link_token

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        global_scope = self.global_scope

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "identity": identity,
                "operations": operations,
                "idempotency_key": idempotency_key,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if if_revision is not UNSET:
            field_dict["if_revision"] = if_revision
        if if_link_token is not UNSET:
            field_dict["if_link_token"] = if_link_token
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch
        if global_scope is not UNSET:
            field_dict["global_scope"] = global_scope

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        identity = d.pop("identity")

        operations = cast(list[Any], d.pop("operations"))

        idempotency_key = d.pop("idempotency_key")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_if_revision(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        if_revision = _parse_if_revision(d.pop("if_revision", UNSET))

        def _parse_if_link_token(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        if_link_token = _parse_if_link_token(d.pop("if_link_token", UNSET))

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        global_scope = d.pop("global_scope", UNSET)

        link_create_request = cls(
            identity=identity,
            operations=operations,
            idempotency_key=idempotency_key,
            project_id=project_id,
            if_revision=if_revision,
            if_link_token=if_link_token,
            claim_epoch=claim_epoch,
            global_scope=global_scope,
        )

        link_create_request.additional_properties = d
        return link_create_request

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
