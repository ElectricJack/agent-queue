from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskSessionAttempt")


@_attrs_define
class TaskSessionAttempt:
    """
    Attributes:
        id (str):
        session_id (str):
        task_id (str):
        harness (str):
        provider (str):
        state (str):
        work_dir (str):
        started_at (float):
        session_started_at (float):
        agent_id (None | str | Unset):
        agent_name (None | str | Unset):
        model (None | str | Unset):
        intelligence_class (None | str | Unset):
        ended_at (float | None | Unset):
        end_reason (None | str | Unset):
        outcome (None | str | Unset):
        session_key (None | str | Unset):
    """

    id: str
    session_id: str
    task_id: str
    harness: str
    provider: str
    state: str
    work_dir: str
    started_at: float
    session_started_at: float
    agent_id: None | str | Unset = UNSET
    agent_name: None | str | Unset = UNSET
    model: None | str | Unset = UNSET
    intelligence_class: None | str | Unset = UNSET
    ended_at: float | None | Unset = UNSET
    end_reason: None | str | Unset = UNSET
    outcome: None | str | Unset = UNSET
    session_key: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        session_id = self.session_id

        task_id = self.task_id

        harness = self.harness

        provider = self.provider

        state = self.state

        work_dir = self.work_dir

        started_at = self.started_at

        session_started_at = self.session_started_at

        agent_id: None | str | Unset
        if isinstance(self.agent_id, Unset):
            agent_id = UNSET
        else:
            agent_id = self.agent_id

        agent_name: None | str | Unset
        if isinstance(self.agent_name, Unset):
            agent_name = UNSET
        else:
            agent_name = self.agent_name

        model: None | str | Unset
        if isinstance(self.model, Unset):
            model = UNSET
        else:
            model = self.model

        intelligence_class: None | str | Unset
        if isinstance(self.intelligence_class, Unset):
            intelligence_class = UNSET
        else:
            intelligence_class = self.intelligence_class

        ended_at: float | None | Unset
        if isinstance(self.ended_at, Unset):
            ended_at = UNSET
        else:
            ended_at = self.ended_at

        end_reason: None | str | Unset
        if isinstance(self.end_reason, Unset):
            end_reason = UNSET
        else:
            end_reason = self.end_reason

        outcome: None | str | Unset
        if isinstance(self.outcome, Unset):
            outcome = UNSET
        else:
            outcome = self.outcome

        session_key: None | str | Unset
        if isinstance(self.session_key, Unset):
            session_key = UNSET
        else:
            session_key = self.session_key

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "session_id": session_id,
                "task_id": task_id,
                "harness": harness,
                "provider": provider,
                "state": state,
                "work_dir": work_dir,
                "started_at": started_at,
                "session_started_at": session_started_at,
            }
        )
        if agent_id is not UNSET:
            field_dict["agent_id"] = agent_id
        if agent_name is not UNSET:
            field_dict["agent_name"] = agent_name
        if model is not UNSET:
            field_dict["model"] = model
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if ended_at is not UNSET:
            field_dict["ended_at"] = ended_at
        if end_reason is not UNSET:
            field_dict["end_reason"] = end_reason
        if outcome is not UNSET:
            field_dict["outcome"] = outcome
        if session_key is not UNSET:
            field_dict["session_key"] = session_key

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        session_id = d.pop("session_id")

        task_id = d.pop("task_id")

        harness = d.pop("harness")

        provider = d.pop("provider")

        state = d.pop("state")

        work_dir = d.pop("work_dir")

        started_at = d.pop("started_at")

        session_started_at = d.pop("session_started_at")

        def _parse_agent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        agent_id = _parse_agent_id(d.pop("agent_id", UNSET))

        def _parse_agent_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        agent_name = _parse_agent_name(d.pop("agent_name", UNSET))

        def _parse_model(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model = _parse_model(d.pop("model", UNSET))

        def _parse_intelligence_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        intelligence_class = _parse_intelligence_class(d.pop("intelligence_class", UNSET))

        def _parse_ended_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        ended_at = _parse_ended_at(d.pop("ended_at", UNSET))

        def _parse_end_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        end_reason = _parse_end_reason(d.pop("end_reason", UNSET))

        def _parse_outcome(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        outcome = _parse_outcome(d.pop("outcome", UNSET))

        def _parse_session_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        session_key = _parse_session_key(d.pop("session_key", UNSET))

        task_session_attempt = cls(
            id=id,
            session_id=session_id,
            task_id=task_id,
            harness=harness,
            provider=provider,
            state=state,
            work_dir=work_dir,
            started_at=started_at,
            session_started_at=session_started_at,
            agent_id=agent_id,
            agent_name=agent_name,
            model=model,
            intelligence_class=intelligence_class,
            ended_at=ended_at,
            end_reason=end_reason,
            outcome=outcome,
            session_key=session_key,
        )

        task_session_attempt.additional_properties = d
        return task_session_attempt

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
