from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ClaimReadinessReason")


@_attrs_define
class ClaimReadinessReason:
    """
    Attributes:
        code (str):
        detail (str):
        ref (None | str | Unset):
        batch_id (None | str | Unset):
        batch_ids (list[str] | None | Unset):
        epic_id (None | str | Unset):
        action (None | str | Unset):
        blocking_task_ids (list[str] | None | Unset):
    """

    code: str
    detail: str
    ref: None | str | Unset = UNSET
    batch_id: None | str | Unset = UNSET
    batch_ids: list[str] | None | Unset = UNSET
    epic_id: None | str | Unset = UNSET
    action: None | str | Unset = UNSET
    blocking_task_ids: list[str] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        code = self.code

        detail = self.detail

        ref: None | str | Unset
        if isinstance(self.ref, Unset):
            ref = UNSET
        else:
            ref = self.ref

        batch_id: None | str | Unset
        if isinstance(self.batch_id, Unset):
            batch_id = UNSET
        else:
            batch_id = self.batch_id

        batch_ids: list[str] | None | Unset
        if isinstance(self.batch_ids, Unset):
            batch_ids = UNSET
        elif isinstance(self.batch_ids, list):
            batch_ids = self.batch_ids

        else:
            batch_ids = self.batch_ids

        epic_id: None | str | Unset
        if isinstance(self.epic_id, Unset):
            epic_id = UNSET
        else:
            epic_id = self.epic_id

        action: None | str | Unset
        if isinstance(self.action, Unset):
            action = UNSET
        else:
            action = self.action

        blocking_task_ids: list[str] | None | Unset
        if isinstance(self.blocking_task_ids, Unset):
            blocking_task_ids = UNSET
        elif isinstance(self.blocking_task_ids, list):
            blocking_task_ids = self.blocking_task_ids

        else:
            blocking_task_ids = self.blocking_task_ids

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "code": code,
                "detail": detail,
            }
        )
        if ref is not UNSET:
            field_dict["ref"] = ref
        if batch_id is not UNSET:
            field_dict["batch_id"] = batch_id
        if batch_ids is not UNSET:
            field_dict["batch_ids"] = batch_ids
        if epic_id is not UNSET:
            field_dict["epic_id"] = epic_id
        if action is not UNSET:
            field_dict["action"] = action
        if blocking_task_ids is not UNSET:
            field_dict["blocking_task_ids"] = blocking_task_ids

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        detail = d.pop("detail")

        def _parse_ref(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        ref = _parse_ref(d.pop("ref", UNSET))

        def _parse_batch_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        batch_id = _parse_batch_id(d.pop("batch_id", UNSET))

        def _parse_batch_ids(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                batch_ids_type_0 = cast(list[str], data)

                return batch_ids_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        batch_ids = _parse_batch_ids(d.pop("batch_ids", UNSET))

        def _parse_epic_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        epic_id = _parse_epic_id(d.pop("epic_id", UNSET))

        def _parse_action(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        action = _parse_action(d.pop("action", UNSET))

        def _parse_blocking_task_ids(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                blocking_task_ids_type_0 = cast(list[str], data)

                return blocking_task_ids_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        blocking_task_ids = _parse_blocking_task_ids(d.pop("blocking_task_ids", UNSET))

        claim_readiness_reason = cls(
            code=code,
            detail=detail,
            ref=ref,
            batch_id=batch_id,
            batch_ids=batch_ids,
            epic_id=epic_id,
            action=action,
            blocking_task_ids=blocking_task_ids,
        )

        claim_readiness_reason.additional_properties = d
        return claim_readiness_reason

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
