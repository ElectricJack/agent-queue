from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.wait_register_args_kind_type_0 import WaitRegisterArgsKindType0
from ..models.wait_register_args_owner_kind_type_0 import WaitRegisterArgsOwnerKindType0
from ..models.wait_register_args_schema_version import WaitRegisterArgsSchemaVersion
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.wait_register_args_condition_type_0 import WaitRegisterArgsConditionType0


T = TypeVar("T", bound="WaitRegisterArgs")


@_attrs_define
class WaitRegisterArgs:
    """
    Attributes:
        idempotency_key (str):
        schema_version (WaitRegisterArgsSchemaVersion | Unset):  Default: WaitRegisterArgsSchemaVersion.VALUE_1.
        project_id (None | str | Unset):
        task_id (None | str | Unset):
        session_id (None | str | Unset):
        kind (None | Unset | WaitRegisterArgsKindType0):
        condition (None | Unset | WaitRegisterArgsConditionType0):
        claimed (None | str | Unset):
        escalate_after_seconds (float | None | Unset):
        owner_kind (None | Unset | WaitRegisterArgsOwnerKindType0):
        owner_id (None | str | Unset):
        ref (None | str | Unset):
        after_seq (int | None | Unset):
        due_at (float | None | Unset):
        timeout (float | None | Unset):
        claim_epoch (int | None | Unset):
    """

    idempotency_key: str
    schema_version: WaitRegisterArgsSchemaVersion | Unset = WaitRegisterArgsSchemaVersion.VALUE_1
    project_id: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    session_id: None | str | Unset = UNSET
    kind: None | Unset | WaitRegisterArgsKindType0 = UNSET
    condition: None | Unset | WaitRegisterArgsConditionType0 = UNSET
    claimed: None | str | Unset = UNSET
    escalate_after_seconds: float | None | Unset = UNSET
    owner_kind: None | Unset | WaitRegisterArgsOwnerKindType0 = UNSET
    owner_id: None | str | Unset = UNSET
    ref: None | str | Unset = UNSET
    after_seq: int | None | Unset = UNSET
    due_at: float | None | Unset = UNSET
    timeout: float | None | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.wait_register_args_condition_type_0 import WaitRegisterArgsConditionType0

        idempotency_key = self.idempotency_key

        schema_version: int | Unset = UNSET
        if not isinstance(self.schema_version, Unset):
            schema_version = self.schema_version.value

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

        kind: None | str | Unset
        if isinstance(self.kind, Unset):
            kind = UNSET
        elif isinstance(self.kind, WaitRegisterArgsKindType0):
            kind = self.kind.value
        else:
            kind = self.kind

        condition: dict[str, Any] | None | Unset
        if isinstance(self.condition, Unset):
            condition = UNSET
        elif isinstance(self.condition, WaitRegisterArgsConditionType0):
            condition = self.condition.to_dict()
        else:
            condition = self.condition

        claimed: None | str | Unset
        if isinstance(self.claimed, Unset):
            claimed = UNSET
        else:
            claimed = self.claimed

        escalate_after_seconds: float | None | Unset
        if isinstance(self.escalate_after_seconds, Unset):
            escalate_after_seconds = UNSET
        else:
            escalate_after_seconds = self.escalate_after_seconds

        owner_kind: None | str | Unset
        if isinstance(self.owner_kind, Unset):
            owner_kind = UNSET
        elif isinstance(self.owner_kind, WaitRegisterArgsOwnerKindType0):
            owner_kind = self.owner_kind.value
        else:
            owner_kind = self.owner_kind

        owner_id: None | str | Unset
        if isinstance(self.owner_id, Unset):
            owner_id = UNSET
        else:
            owner_id = self.owner_id

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

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "idempotency_key": idempotency_key,
            }
        )
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if session_id is not UNSET:
            field_dict["session_id"] = session_id
        if kind is not UNSET:
            field_dict["kind"] = kind
        if condition is not UNSET:
            field_dict["condition"] = condition
        if claimed is not UNSET:
            field_dict["claimed"] = claimed
        if escalate_after_seconds is not UNSET:
            field_dict["escalate_after_seconds"] = escalate_after_seconds
        if owner_kind is not UNSET:
            field_dict["owner_kind"] = owner_kind
        if owner_id is not UNSET:
            field_dict["owner_id"] = owner_id
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

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.wait_register_args_condition_type_0 import WaitRegisterArgsConditionType0

        d = dict(src_dict)
        idempotency_key = d.pop("idempotency_key")

        _schema_version = d.pop("schema_version", UNSET)
        schema_version: WaitRegisterArgsSchemaVersion | Unset
        if isinstance(_schema_version, Unset):
            schema_version = UNSET
        else:
            schema_version = WaitRegisterArgsSchemaVersion(_schema_version)

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

        def _parse_kind(data: object) -> None | Unset | WaitRegisterArgsKindType0:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                kind_type_0 = WaitRegisterArgsKindType0(data)

                return kind_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitRegisterArgsKindType0, data)

        kind = _parse_kind(d.pop("kind", UNSET))

        def _parse_condition(data: object) -> None | Unset | WaitRegisterArgsConditionType0:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                condition_type_0 = WaitRegisterArgsConditionType0.from_dict(data)

                return condition_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitRegisterArgsConditionType0, data)

        condition = _parse_condition(d.pop("condition", UNSET))

        def _parse_claimed(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        claimed = _parse_claimed(d.pop("claimed", UNSET))

        def _parse_escalate_after_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        escalate_after_seconds = _parse_escalate_after_seconds(d.pop("escalate_after_seconds", UNSET))

        def _parse_owner_kind(data: object) -> None | Unset | WaitRegisterArgsOwnerKindType0:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                owner_kind_type_0 = WaitRegisterArgsOwnerKindType0(data)

                return owner_kind_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitRegisterArgsOwnerKindType0, data)

        owner_kind = _parse_owner_kind(d.pop("owner_kind", UNSET))

        def _parse_owner_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        owner_id = _parse_owner_id(d.pop("owner_id", UNSET))

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

        wait_register_args = cls(
            idempotency_key=idempotency_key,
            schema_version=schema_version,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            kind=kind,
            condition=condition,
            claimed=claimed,
            escalate_after_seconds=escalate_after_seconds,
            owner_kind=owner_kind,
            owner_id=owner_id,
            ref=ref,
            after_seq=after_seq,
            due_at=due_at,
            timeout=timeout,
            claim_epoch=claim_epoch,
        )

        return wait_register_args
