from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.agent_wait_record_kind import AgentWaitRecordKind
from ..models.agent_wait_record_owner_kind import AgentWaitRecordOwnerKind
from ..models.agent_wait_record_state import AgentWaitRecordState
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.agent_wait_record_digest_type_0 import AgentWaitRecordDigestType0
    from ..models.agent_wait_record_match import AgentWaitRecordMatch


T = TypeVar("T", bound="AgentWaitRecord")


@_attrs_define
class AgentWaitRecord:
    """Persisted wait/result shape shared by contracts and generated clients.

    Attributes:
        id (str):
        project_id (str):
        owner_kind (AgentWaitRecordOwnerKind):
        owner_id (str):
        session_id (str):
        session_instance_token (str):
        claim_epoch (int):
        kind (AgentWaitRecordKind):
        match (AgentWaitRecordMatch):
        state (AgentWaitRecordState):
        version (int):
        created_at (float):
        deadline_at (float):
        idempotency_key (str):
        resolved_at (float | None | Unset):
        wait_resumed_at (float | None | Unset):
        checked_at (float | Unset):  Default: 0.0.
        result_ref (None | str | Unset):
        digest (AgentWaitRecordDigestType0 | None | Unset):
        result_message_id (None | str | Unset):
    """

    id: str
    project_id: str
    owner_kind: AgentWaitRecordOwnerKind
    owner_id: str
    session_id: str
    session_instance_token: str
    claim_epoch: int
    kind: AgentWaitRecordKind
    match: AgentWaitRecordMatch
    state: AgentWaitRecordState
    version: int
    created_at: float
    deadline_at: float
    idempotency_key: str
    resolved_at: float | None | Unset = UNSET
    wait_resumed_at: float | None | Unset = UNSET
    checked_at: float | Unset = 0.0
    result_ref: None | str | Unset = UNSET
    digest: AgentWaitRecordDigestType0 | None | Unset = UNSET
    result_message_id: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_wait_record_digest_type_0 import AgentWaitRecordDigestType0

        id = self.id

        project_id = self.project_id

        owner_kind = self.owner_kind.value

        owner_id = self.owner_id

        session_id = self.session_id

        session_instance_token = self.session_instance_token

        claim_epoch = self.claim_epoch

        kind = self.kind.value

        match = self.match.to_dict()

        state = self.state.value

        version = self.version

        created_at = self.created_at

        deadline_at = self.deadline_at

        idempotency_key = self.idempotency_key

        resolved_at: float | None | Unset
        if isinstance(self.resolved_at, Unset):
            resolved_at = UNSET
        else:
            resolved_at = self.resolved_at

        wait_resumed_at: float | None | Unset
        if isinstance(self.wait_resumed_at, Unset):
            wait_resumed_at = UNSET
        else:
            wait_resumed_at = self.wait_resumed_at

        checked_at = self.checked_at

        result_ref: None | str | Unset
        if isinstance(self.result_ref, Unset):
            result_ref = UNSET
        else:
            result_ref = self.result_ref

        digest: dict[str, Any] | None | Unset
        if isinstance(self.digest, Unset):
            digest = UNSET
        elif isinstance(self.digest, AgentWaitRecordDigestType0):
            digest = self.digest.to_dict()
        else:
            digest = self.digest

        result_message_id: None | str | Unset
        if isinstance(self.result_message_id, Unset):
            result_message_id = UNSET
        else:
            result_message_id = self.result_message_id

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "id": id,
                "project_id": project_id,
                "owner_kind": owner_kind,
                "owner_id": owner_id,
                "session_id": session_id,
                "session_instance_token": session_instance_token,
                "claim_epoch": claim_epoch,
                "kind": kind,
                "match": match,
                "state": state,
                "version": version,
                "created_at": created_at,
                "deadline_at": deadline_at,
                "idempotency_key": idempotency_key,
            }
        )
        if resolved_at is not UNSET:
            field_dict["resolved_at"] = resolved_at
        if wait_resumed_at is not UNSET:
            field_dict["wait_resumed_at"] = wait_resumed_at
        if checked_at is not UNSET:
            field_dict["checked_at"] = checked_at
        if result_ref is not UNSET:
            field_dict["result_ref"] = result_ref
        if digest is not UNSET:
            field_dict["digest"] = digest
        if result_message_id is not UNSET:
            field_dict["result_message_id"] = result_message_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_wait_record_digest_type_0 import AgentWaitRecordDigestType0
        from ..models.agent_wait_record_match import AgentWaitRecordMatch

        d = dict(src_dict)
        id = d.pop("id")

        project_id = d.pop("project_id")

        owner_kind = AgentWaitRecordOwnerKind(d.pop("owner_kind"))

        owner_id = d.pop("owner_id")

        session_id = d.pop("session_id")

        session_instance_token = d.pop("session_instance_token")

        claim_epoch = d.pop("claim_epoch")

        kind = AgentWaitRecordKind(d.pop("kind"))

        match = AgentWaitRecordMatch.from_dict(d.pop("match"))

        state = AgentWaitRecordState(d.pop("state"))

        version = d.pop("version")

        created_at = d.pop("created_at")

        deadline_at = d.pop("deadline_at")

        idempotency_key = d.pop("idempotency_key")

        def _parse_resolved_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        resolved_at = _parse_resolved_at(d.pop("resolved_at", UNSET))

        def _parse_wait_resumed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        wait_resumed_at = _parse_wait_resumed_at(d.pop("wait_resumed_at", UNSET))

        checked_at = d.pop("checked_at", UNSET)

        def _parse_result_ref(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        result_ref = _parse_result_ref(d.pop("result_ref", UNSET))

        def _parse_digest(data: object) -> AgentWaitRecordDigestType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                digest_type_0 = AgentWaitRecordDigestType0.from_dict(data)

                return digest_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AgentWaitRecordDigestType0 | None | Unset, data)

        digest = _parse_digest(d.pop("digest", UNSET))

        def _parse_result_message_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        result_message_id = _parse_result_message_id(d.pop("result_message_id", UNSET))

        agent_wait_record = cls(
            id=id,
            project_id=project_id,
            owner_kind=owner_kind,
            owner_id=owner_id,
            session_id=session_id,
            session_instance_token=session_instance_token,
            claim_epoch=claim_epoch,
            kind=kind,
            match=match,
            state=state,
            version=version,
            created_at=created_at,
            deadline_at=deadline_at,
            idempotency_key=idempotency_key,
            resolved_at=resolved_at,
            wait_resumed_at=wait_resumed_at,
            checked_at=checked_at,
            result_ref=result_ref,
            digest=digest,
            result_message_id=result_message_id,
        )

        return agent_wait_record
