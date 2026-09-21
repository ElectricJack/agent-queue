from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_transition_detail import ProviderTransitionDetail


T = TypeVar("T", bound="ProviderTransition")


@_attrs_define
class ProviderTransition:
    """One change of effective state, from the audit trail.

    Attributes:
        provider (str):
        from_state (str):
        to_state (str):
        generation (int):
        at (float):
        id (int | None | Unset):
        reason_code (str | Unset):  Default: ''.
        reason (str | Unset):  Default: ''.
        until (float | None | Unset):
        actor (str | Unset):  Default: 'system'.
        detail (ProviderTransitionDetail | Unset):
    """

    provider: str
    from_state: str
    to_state: str
    generation: int
    at: float
    id: int | None | Unset = UNSET
    reason_code: str | Unset = ""
    reason: str | Unset = ""
    until: float | None | Unset = UNSET
    actor: str | Unset = "system"
    detail: ProviderTransitionDetail | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        provider = self.provider

        from_state = self.from_state

        to_state = self.to_state

        generation = self.generation

        at = self.at

        id: int | None | Unset
        if isinstance(self.id, Unset):
            id = UNSET
        else:
            id = self.id

        reason_code = self.reason_code

        reason = self.reason

        until: float | None | Unset
        if isinstance(self.until, Unset):
            until = UNSET
        else:
            until = self.until

        actor = self.actor

        detail: dict[str, Any] | Unset = UNSET
        if not isinstance(self.detail, Unset):
            detail = self.detail.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "provider": provider,
                "from_state": from_state,
                "to_state": to_state,
                "generation": generation,
                "at": at,
            }
        )
        if id is not UNSET:
            field_dict["id"] = id
        if reason_code is not UNSET:
            field_dict["reason_code"] = reason_code
        if reason is not UNSET:
            field_dict["reason"] = reason
        if until is not UNSET:
            field_dict["until"] = until
        if actor is not UNSET:
            field_dict["actor"] = actor
        if detail is not UNSET:
            field_dict["detail"] = detail

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_transition_detail import ProviderTransitionDetail

        d = dict(src_dict)
        provider = d.pop("provider")

        from_state = d.pop("from_state")

        to_state = d.pop("to_state")

        generation = d.pop("generation")

        at = d.pop("at")

        def _parse_id(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        id = _parse_id(d.pop("id", UNSET))

        reason_code = d.pop("reason_code", UNSET)

        reason = d.pop("reason", UNSET)

        def _parse_until(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        until = _parse_until(d.pop("until", UNSET))

        actor = d.pop("actor", UNSET)

        _detail = d.pop("detail", UNSET)
        detail: ProviderTransitionDetail | Unset
        if isinstance(_detail, Unset):
            detail = UNSET
        else:
            detail = ProviderTransitionDetail.from_dict(_detail)

        provider_transition = cls(
            provider=provider,
            from_state=from_state,
            to_state=to_state,
            generation=generation,
            at=at,
            id=id,
            reason_code=reason_code,
            reason=reason,
            until=until,
            actor=actor,
            detail=detail,
        )

        provider_transition.additional_properties = d
        return provider_transition

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
