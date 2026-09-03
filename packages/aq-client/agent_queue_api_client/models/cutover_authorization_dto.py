from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.cutover_authorization_dto_role_type_0 import CutoverAuthorizationDTORoleType0
from ..types import UNSET, Unset

T = TypeVar("T", bound="CutoverAuthorizationDTO")


@_attrs_define
class CutoverAuthorizationDTO:
    """One G2 signature: who the server saw (``actor``) and who the human
    declared themselves to be (``signed_by``), in one of the two roles.

        Attributes:
            event_id (None | str | Unset):
            at (float | None | Unset):
            actor (None | str | Unset):
            role (CutoverAuthorizationDTORoleType0 | None | Unset):
            signed_by (None | str | Unset):
    """

    event_id: None | str | Unset = UNSET
    at: float | None | Unset = UNSET
    actor: None | str | Unset = UNSET
    role: CutoverAuthorizationDTORoleType0 | None | Unset = UNSET
    signed_by: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        event_id: None | str | Unset
        if isinstance(self.event_id, Unset):
            event_id = UNSET
        else:
            event_id = self.event_id

        at: float | None | Unset
        if isinstance(self.at, Unset):
            at = UNSET
        else:
            at = self.at

        actor: None | str | Unset
        if isinstance(self.actor, Unset):
            actor = UNSET
        else:
            actor = self.actor

        role: None | str | Unset
        if isinstance(self.role, Unset):
            role = UNSET
        elif isinstance(self.role, CutoverAuthorizationDTORoleType0):
            role = self.role.value
        else:
            role = self.role

        signed_by: None | str | Unset
        if isinstance(self.signed_by, Unset):
            signed_by = UNSET
        else:
            signed_by = self.signed_by

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if event_id is not UNSET:
            field_dict["event_id"] = event_id
        if at is not UNSET:
            field_dict["at"] = at
        if actor is not UNSET:
            field_dict["actor"] = actor
        if role is not UNSET:
            field_dict["role"] = role
        if signed_by is not UNSET:
            field_dict["signed_by"] = signed_by

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_event_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        event_id = _parse_event_id(d.pop("event_id", UNSET))

        def _parse_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        at = _parse_at(d.pop("at", UNSET))

        def _parse_actor(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        actor = _parse_actor(d.pop("actor", UNSET))

        def _parse_role(data: object) -> CutoverAuthorizationDTORoleType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                role_type_0 = CutoverAuthorizationDTORoleType0(data)

                return role_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CutoverAuthorizationDTORoleType0 | None | Unset, data)

        role = _parse_role(d.pop("role", UNSET))

        def _parse_signed_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        signed_by = _parse_signed_by(d.pop("signed_by", UNSET))

        cutover_authorization_dto = cls(
            event_id=event_id,
            at=at,
            actor=actor,
            role=role,
            signed_by=signed_by,
        )

        return cutover_authorization_dto
