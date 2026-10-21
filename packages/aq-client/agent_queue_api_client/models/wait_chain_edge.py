from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitChainEdge")


@_attrs_define
class WaitChainEdge:
    """
    Attributes:
        from_ (str):
        to (str):
        relation (str):
        id (None | str | Unset):
        wait_id (None | str | Unset):
        source_revision (int | None | str | Unset):
        active (bool | None | Unset):
        created_at (float | None | Unset):
        cleared_at (float | None | Unset):
    """

    from_: str
    to: str
    relation: str
    id: None | str | Unset = UNSET
    wait_id: None | str | Unset = UNSET
    source_revision: int | None | str | Unset = UNSET
    active: bool | None | Unset = UNSET
    created_at: float | None | Unset = UNSET
    cleared_at: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from_ = self.from_

        to = self.to

        relation = self.relation

        id: None | str | Unset
        if isinstance(self.id, Unset):
            id = UNSET
        else:
            id = self.id

        wait_id: None | str | Unset
        if isinstance(self.wait_id, Unset):
            wait_id = UNSET
        else:
            wait_id = self.wait_id

        source_revision: int | None | str | Unset
        if isinstance(self.source_revision, Unset):
            source_revision = UNSET
        else:
            source_revision = self.source_revision

        active: bool | None | Unset
        if isinstance(self.active, Unset):
            active = UNSET
        else:
            active = self.active

        created_at: float | None | Unset
        if isinstance(self.created_at, Unset):
            created_at = UNSET
        else:
            created_at = self.created_at

        cleared_at: float | None | Unset
        if isinstance(self.cleared_at, Unset):
            cleared_at = UNSET
        else:
            cleared_at = self.cleared_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "from": from_,
                "to": to,
                "relation": relation,
            }
        )
        if id is not UNSET:
            field_dict["id"] = id
        if wait_id is not UNSET:
            field_dict["wait_id"] = wait_id
        if source_revision is not UNSET:
            field_dict["source_revision"] = source_revision
        if active is not UNSET:
            field_dict["active"] = active
        if created_at is not UNSET:
            field_dict["created_at"] = created_at
        if cleared_at is not UNSET:
            field_dict["cleared_at"] = cleared_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        from_ = d.pop("from")

        to = d.pop("to")

        relation = d.pop("relation")

        def _parse_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        id = _parse_id(d.pop("id", UNSET))

        def _parse_wait_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        wait_id = _parse_wait_id(d.pop("wait_id", UNSET))

        def _parse_source_revision(data: object) -> int | None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | str | Unset, data)

        source_revision = _parse_source_revision(d.pop("source_revision", UNSET))

        def _parse_active(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        active = _parse_active(d.pop("active", UNSET))

        def _parse_created_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        created_at = _parse_created_at(d.pop("created_at", UNSET))

        def _parse_cleared_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        cleared_at = _parse_cleared_at(d.pop("cleared_at", UNSET))

        wait_chain_edge = cls(
            from_=from_,
            to=to,
            relation=relation,
            id=id,
            wait_id=wait_id,
            source_revision=source_revision,
            active=active,
            created_at=created_at,
            cleared_at=cleared_at,
        )

        wait_chain_edge.additional_properties = d
        return wait_chain_edge

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
