from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="KnowledgeContextDeliverRequest")


@_attrs_define
class KnowledgeContextDeliverRequest:
    """
    Attributes:
        bundle_id (str):
        transport (str):
        idempotency_key (str):
        rendered_sha256 (str):
        project_id (None | str | Unset):
        state (str | Unset):  Default: 'delivered'.
        claim_epoch (int | None | Unset):
    """

    bundle_id: str
    transport: str
    idempotency_key: str
    rendered_sha256: str
    project_id: None | str | Unset = UNSET
    state: str | Unset = "delivered"
    claim_epoch: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        bundle_id = self.bundle_id

        transport = self.transport

        idempotency_key = self.idempotency_key

        rendered_sha256 = self.rendered_sha256

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        state = self.state

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "bundle_id": bundle_id,
                "transport": transport,
                "idempotency_key": idempotency_key,
                "rendered_sha256": rendered_sha256,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if state is not UNSET:
            field_dict["state"] = state
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        bundle_id = d.pop("bundle_id")

        transport = d.pop("transport")

        idempotency_key = d.pop("idempotency_key")

        rendered_sha256 = d.pop("rendered_sha256")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        state = d.pop("state", UNSET)

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        knowledge_context_deliver_request = cls(
            bundle_id=bundle_id,
            transport=transport,
            idempotency_key=idempotency_key,
            rendered_sha256=rendered_sha256,
            project_id=project_id,
            state=state,
            claim_epoch=claim_epoch,
        )

        knowledge_context_deliver_request.additional_properties = d
        return knowledge_context_deliver_request

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
