from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskReroute")


@_attrs_define
class TaskReroute:
    """The newest ``task_reroutes`` row for a task (provider-failover D17).

    ``undoable`` is true while the task carries an un-undone re-route and no
    worker holds it -- what ``aq provider reroute-undo --task-id`` would accept.

        Attributes:
            id (int):
            reason_code (str):
            at (float):
            from_profile_id (None | str | Unset):
            to_profile_id (None | str | Unset):
            from_provider (str | Unset):  Default: ''.
            to_provider (str | Unset):  Default: ''.
            intelligence_class (None | str | Unset):
            provider_state (str | Unset):  Default: ''.
            batch_id (None | str | Unset):
            actor (str | Unset):  Default: ''.
            undone_at (float | None | Unset):
            undoable (bool | Unset):  Default: False.
    """

    id: int
    reason_code: str
    at: float
    from_profile_id: None | str | Unset = UNSET
    to_profile_id: None | str | Unset = UNSET
    from_provider: str | Unset = ""
    to_provider: str | Unset = ""
    intelligence_class: None | str | Unset = UNSET
    provider_state: str | Unset = ""
    batch_id: None | str | Unset = UNSET
    actor: str | Unset = ""
    undone_at: float | None | Unset = UNSET
    undoable: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        reason_code = self.reason_code

        at = self.at

        from_profile_id: None | str | Unset
        if isinstance(self.from_profile_id, Unset):
            from_profile_id = UNSET
        else:
            from_profile_id = self.from_profile_id

        to_profile_id: None | str | Unset
        if isinstance(self.to_profile_id, Unset):
            to_profile_id = UNSET
        else:
            to_profile_id = self.to_profile_id

        from_provider = self.from_provider

        to_provider = self.to_provider

        intelligence_class: None | str | Unset
        if isinstance(self.intelligence_class, Unset):
            intelligence_class = UNSET
        else:
            intelligence_class = self.intelligence_class

        provider_state = self.provider_state

        batch_id: None | str | Unset
        if isinstance(self.batch_id, Unset):
            batch_id = UNSET
        else:
            batch_id = self.batch_id

        actor = self.actor

        undone_at: float | None | Unset
        if isinstance(self.undone_at, Unset):
            undone_at = UNSET
        else:
            undone_at = self.undone_at

        undoable = self.undoable

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "reason_code": reason_code,
                "at": at,
            }
        )
        if from_profile_id is not UNSET:
            field_dict["from_profile_id"] = from_profile_id
        if to_profile_id is not UNSET:
            field_dict["to_profile_id"] = to_profile_id
        if from_provider is not UNSET:
            field_dict["from_provider"] = from_provider
        if to_provider is not UNSET:
            field_dict["to_provider"] = to_provider
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if provider_state is not UNSET:
            field_dict["provider_state"] = provider_state
        if batch_id is not UNSET:
            field_dict["batch_id"] = batch_id
        if actor is not UNSET:
            field_dict["actor"] = actor
        if undone_at is not UNSET:
            field_dict["undone_at"] = undone_at
        if undoable is not UNSET:
            field_dict["undoable"] = undoable

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        reason_code = d.pop("reason_code")

        at = d.pop("at")

        def _parse_from_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        from_profile_id = _parse_from_profile_id(d.pop("from_profile_id", UNSET))

        def _parse_to_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        to_profile_id = _parse_to_profile_id(d.pop("to_profile_id", UNSET))

        from_provider = d.pop("from_provider", UNSET)

        to_provider = d.pop("to_provider", UNSET)

        def _parse_intelligence_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        intelligence_class = _parse_intelligence_class(d.pop("intelligence_class", UNSET))

        provider_state = d.pop("provider_state", UNSET)

        def _parse_batch_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        batch_id = _parse_batch_id(d.pop("batch_id", UNSET))

        actor = d.pop("actor", UNSET)

        def _parse_undone_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        undone_at = _parse_undone_at(d.pop("undone_at", UNSET))

        undoable = d.pop("undoable", UNSET)

        task_reroute = cls(
            id=id,
            reason_code=reason_code,
            at=at,
            from_profile_id=from_profile_id,
            to_profile_id=to_profile_id,
            from_provider=from_provider,
            to_provider=to_provider,
            intelligence_class=intelligence_class,
            provider_state=provider_state,
            batch_id=batch_id,
            actor=actor,
            undone_at=undone_at,
            undoable=undoable,
        )

        task_reroute.additional_properties = d
        return task_reroute

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
