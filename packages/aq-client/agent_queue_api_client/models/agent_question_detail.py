from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="AgentQuestionDetail")


@_attrs_define
class AgentQuestionDetail:
    """Durable question returned by scoped list/answer/escalate commands.

    Attributes:
        id (str):
        question (str):
        state (str):
        requires_human (bool):
        created_at (float):
        session_id (str):
        session_name (str):
        instance_token (str):
        task_id (str):
        project_id (str):
        agent_id (str):
        turn_id (str):
        claim_epoch (int):
        updated_at (float):
        source_ts (float):
        answer (None | str | Unset):
        answered_by (None | str | Unset):
        discord_channel_id (None | str | Unset):
        discord_message_id (None | str | Unset):
        supervisor_routed_at (float | None | Unset):
        notification_next_at (float | Unset):  Default: 0.0.
        notification_attempts (int | Unset):  Default: 0.
        delivery_token (None | str | Unset):
        delivery_lease_until (float | None | Unset):
        delivered_at (float | None | Unset):
        reason (None | str | Unset):
    """

    id: str
    question: str
    state: str
    requires_human: bool
    created_at: float
    session_id: str
    session_name: str
    instance_token: str
    task_id: str
    project_id: str
    agent_id: str
    turn_id: str
    claim_epoch: int
    updated_at: float
    source_ts: float
    answer: None | str | Unset = UNSET
    answered_by: None | str | Unset = UNSET
    discord_channel_id: None | str | Unset = UNSET
    discord_message_id: None | str | Unset = UNSET
    supervisor_routed_at: float | None | Unset = UNSET
    notification_next_at: float | Unset = 0.0
    notification_attempts: int | Unset = 0
    delivery_token: None | str | Unset = UNSET
    delivery_lease_until: float | None | Unset = UNSET
    delivered_at: float | None | Unset = UNSET
    reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        question = self.question

        state = self.state

        requires_human = self.requires_human

        created_at = self.created_at

        session_id = self.session_id

        session_name = self.session_name

        instance_token = self.instance_token

        task_id = self.task_id

        project_id = self.project_id

        agent_id = self.agent_id

        turn_id = self.turn_id

        claim_epoch = self.claim_epoch

        updated_at = self.updated_at

        source_ts = self.source_ts

        answer: None | str | Unset
        if isinstance(self.answer, Unset):
            answer = UNSET
        else:
            answer = self.answer

        answered_by: None | str | Unset
        if isinstance(self.answered_by, Unset):
            answered_by = UNSET
        else:
            answered_by = self.answered_by

        discord_channel_id: None | str | Unset
        if isinstance(self.discord_channel_id, Unset):
            discord_channel_id = UNSET
        else:
            discord_channel_id = self.discord_channel_id

        discord_message_id: None | str | Unset
        if isinstance(self.discord_message_id, Unset):
            discord_message_id = UNSET
        else:
            discord_message_id = self.discord_message_id

        supervisor_routed_at: float | None | Unset
        if isinstance(self.supervisor_routed_at, Unset):
            supervisor_routed_at = UNSET
        else:
            supervisor_routed_at = self.supervisor_routed_at

        notification_next_at = self.notification_next_at

        notification_attempts = self.notification_attempts

        delivery_token: None | str | Unset
        if isinstance(self.delivery_token, Unset):
            delivery_token = UNSET
        else:
            delivery_token = self.delivery_token

        delivery_lease_until: float | None | Unset
        if isinstance(self.delivery_lease_until, Unset):
            delivery_lease_until = UNSET
        else:
            delivery_lease_until = self.delivery_lease_until

        delivered_at: float | None | Unset
        if isinstance(self.delivered_at, Unset):
            delivered_at = UNSET
        else:
            delivered_at = self.delivered_at

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "question": question,
                "state": state,
                "requires_human": requires_human,
                "created_at": created_at,
                "session_id": session_id,
                "session_name": session_name,
                "instance_token": instance_token,
                "task_id": task_id,
                "project_id": project_id,
                "agent_id": agent_id,
                "turn_id": turn_id,
                "claim_epoch": claim_epoch,
                "updated_at": updated_at,
                "source_ts": source_ts,
            }
        )
        if answer is not UNSET:
            field_dict["answer"] = answer
        if answered_by is not UNSET:
            field_dict["answered_by"] = answered_by
        if discord_channel_id is not UNSET:
            field_dict["discord_channel_id"] = discord_channel_id
        if discord_message_id is not UNSET:
            field_dict["discord_message_id"] = discord_message_id
        if supervisor_routed_at is not UNSET:
            field_dict["supervisor_routed_at"] = supervisor_routed_at
        if notification_next_at is not UNSET:
            field_dict["notification_next_at"] = notification_next_at
        if notification_attempts is not UNSET:
            field_dict["notification_attempts"] = notification_attempts
        if delivery_token is not UNSET:
            field_dict["delivery_token"] = delivery_token
        if delivery_lease_until is not UNSET:
            field_dict["delivery_lease_until"] = delivery_lease_until
        if delivered_at is not UNSET:
            field_dict["delivered_at"] = delivered_at
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        question = d.pop("question")

        state = d.pop("state")

        requires_human = d.pop("requires_human")

        created_at = d.pop("created_at")

        session_id = d.pop("session_id")

        session_name = d.pop("session_name")

        instance_token = d.pop("instance_token")

        task_id = d.pop("task_id")

        project_id = d.pop("project_id")

        agent_id = d.pop("agent_id")

        turn_id = d.pop("turn_id")

        claim_epoch = d.pop("claim_epoch")

        updated_at = d.pop("updated_at")

        source_ts = d.pop("source_ts")

        def _parse_answer(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        answer = _parse_answer(d.pop("answer", UNSET))

        def _parse_answered_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        answered_by = _parse_answered_by(d.pop("answered_by", UNSET))

        def _parse_discord_channel_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        discord_channel_id = _parse_discord_channel_id(d.pop("discord_channel_id", UNSET))

        def _parse_discord_message_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        discord_message_id = _parse_discord_message_id(d.pop("discord_message_id", UNSET))

        def _parse_supervisor_routed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        supervisor_routed_at = _parse_supervisor_routed_at(d.pop("supervisor_routed_at", UNSET))

        notification_next_at = d.pop("notification_next_at", UNSET)

        notification_attempts = d.pop("notification_attempts", UNSET)

        def _parse_delivery_token(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        delivery_token = _parse_delivery_token(d.pop("delivery_token", UNSET))

        def _parse_delivery_lease_until(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        delivery_lease_until = _parse_delivery_lease_until(d.pop("delivery_lease_until", UNSET))

        def _parse_delivered_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        delivered_at = _parse_delivered_at(d.pop("delivered_at", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        agent_question_detail = cls(
            id=id,
            question=question,
            state=state,
            requires_human=requires_human,
            created_at=created_at,
            session_id=session_id,
            session_name=session_name,
            instance_token=instance_token,
            task_id=task_id,
            project_id=project_id,
            agent_id=agent_id,
            turn_id=turn_id,
            claim_epoch=claim_epoch,
            updated_at=updated_at,
            source_ts=source_ts,
            answer=answer,
            answered_by=answered_by,
            discord_channel_id=discord_channel_id,
            discord_message_id=discord_message_id,
            supervisor_routed_at=supervisor_routed_at,
            notification_next_at=notification_next_at,
            notification_attempts=notification_attempts,
            delivery_token=delivery_token,
            delivery_lease_until=delivery_lease_until,
            delivered_at=delivered_at,
            reason=reason,
        )

        agent_question_detail.additional_properties = d
        return agent_question_detail

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
