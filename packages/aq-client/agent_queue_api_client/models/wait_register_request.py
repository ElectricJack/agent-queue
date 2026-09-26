from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitRegisterRequest")


@_attrs_define
class WaitRegisterRequest:
    """
    Attributes:
        kind (str):
        idempotency_key (str):
        ref (None | str | Unset):
        after_seq (int | None | Unset):
        due_at (float | None | Unset):
        timeout (float | None | Unset):
        claim_epoch (int | None | Unset):
        project_id (None | str | Unset):
        task_id (None | str | Unset):
        session_id (None | str | Unset):
    """

    kind: str
    idempotency_key: str
    ref: None | str | Unset = UNSET
    after_seq: int | None | Unset = UNSET
    due_at: float | None | Unset = UNSET
    timeout: float | None | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET
    project_id: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    session_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind

        idempotency_key = self.idempotency_key

        ref: None | str | Unset
        if isinstance(self.ref, Unset):
            ref = UNSET
        else:
            ref = self.ref

        after_seq: int | None | Unset
        if isinstance(self.after_seq, Unset):
            after_seq = UNSET
        else:
            after_seq = self.after_seq

        due_at: float | None | Unset
        if isinstance(self.due_at, Unset):
            due_at = UNSET
        else:
            due_at = self.due_at

        timeout: float | None | Unset
        if isinstance(self.timeout, Unset):
            timeout = UNSET
        else:
            timeout = self.timeout

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        session_id: None | str | Unset
        if isinstance(self.session_id, Unset):
            session_id = UNSET
        else:
            session_id = self.session_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
                "idempotency_key": idempotency_key,
            }
        )
        if ref is not UNSET:
            field_dict["ref"] = ref
        if after_seq is not UNSET:
            field_dict["after_seq"] = after_seq
        if due_at is not UNSET:
            field_dict["due_at"] = due_at
        if timeout is not UNSET:
            field_dict["timeout"] = timeout
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if session_id is not UNSET:
            field_dict["session_id"] = session_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = d.pop("kind")

        idempotency_key = d.pop("idempotency_key")

        def _parse_ref(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        ref = _parse_ref(d.pop("ref", UNSET))

        def _parse_after_seq(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        after_seq = _parse_after_seq(d.pop("after_seq", UNSET))

        def _parse_due_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        due_at = _parse_due_at(d.pop("due_at", UNSET))

        def _parse_timeout(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        timeout = _parse_timeout(d.pop("timeout", UNSET))

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_session_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        session_id = _parse_session_id(d.pop("session_id", UNSET))

        wait_register_request = cls(
            kind=kind,
            idempotency_key=idempotency_key,
            ref=ref,
            after_seq=after_seq,
            due_at=due_at,
            timeout=timeout,
            claim_epoch=claim_epoch,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
        )

        wait_register_request.additional_properties = d
        return wait_register_request

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
