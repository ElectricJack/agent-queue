from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.workspace_owner_session import WorkspaceOwnerSession
    from ..models.workspace_owner_task import WorkspaceOwnerTask


T = TypeVar("T", bound="WorkspaceOwner")


@_attrs_define
class WorkspaceOwner:
    """Owner identity as the database proves it (``src/workspace_listing.py``).

    Attributes:
        lock_state (str):
        facts (str):
        classification (str):
        agent_id (None | str | Unset):
        task_id (None | str | Unset):
        locked_at (float | None | Unset):
        task (None | Unset | WorkspaceOwnerTask):
        session (None | Unset | WorkspaceOwnerSession):
        sessions_matched (int | None | Unset):
        fence (None | str | Unset):
        session_matches_claim (bool | None | Unset):
    """

    lock_state: str
    facts: str
    classification: str
    agent_id: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    locked_at: float | None | Unset = UNSET
    task: None | Unset | WorkspaceOwnerTask = UNSET
    session: None | Unset | WorkspaceOwnerSession = UNSET
    sessions_matched: int | None | Unset = UNSET
    fence: None | str | Unset = UNSET
    session_matches_claim: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.workspace_owner_session import WorkspaceOwnerSession
        from ..models.workspace_owner_task import WorkspaceOwnerTask

        lock_state = self.lock_state

        facts = self.facts

        classification = self.classification

        agent_id: None | str | Unset
        if isinstance(self.agent_id, Unset):
            agent_id = UNSET
        else:
            agent_id = self.agent_id

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        locked_at: float | None | Unset
        if isinstance(self.locked_at, Unset):
            locked_at = UNSET
        else:
            locked_at = self.locked_at

        task: dict[str, Any] | None | Unset
        if isinstance(self.task, Unset):
            task = UNSET
        elif isinstance(self.task, WorkspaceOwnerTask):
            task = self.task.to_dict()
        else:
            task = self.task

        session: dict[str, Any] | None | Unset
        if isinstance(self.session, Unset):
            session = UNSET
        elif isinstance(self.session, WorkspaceOwnerSession):
            session = self.session.to_dict()
        else:
            session = self.session

        sessions_matched: int | None | Unset
        if isinstance(self.sessions_matched, Unset):
            sessions_matched = UNSET
        else:
            sessions_matched = self.sessions_matched

        fence: None | str | Unset
        if isinstance(self.fence, Unset):
            fence = UNSET
        else:
            fence = self.fence

        session_matches_claim: bool | None | Unset
        if isinstance(self.session_matches_claim, Unset):
            session_matches_claim = UNSET
        else:
            session_matches_claim = self.session_matches_claim

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "lock_state": lock_state,
                "facts": facts,
                "classification": classification,
            }
        )
        if agent_id is not UNSET:
            field_dict["agent_id"] = agent_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if locked_at is not UNSET:
            field_dict["locked_at"] = locked_at
        if task is not UNSET:
            field_dict["task"] = task
        if session is not UNSET:
            field_dict["session"] = session
        if sessions_matched is not UNSET:
            field_dict["sessions_matched"] = sessions_matched
        if fence is not UNSET:
            field_dict["fence"] = fence
        if session_matches_claim is not UNSET:
            field_dict["session_matches_claim"] = session_matches_claim

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.workspace_owner_session import WorkspaceOwnerSession
        from ..models.workspace_owner_task import WorkspaceOwnerTask

        d = dict(src_dict)
        lock_state = d.pop("lock_state")

        facts = d.pop("facts")

        classification = d.pop("classification")

        def _parse_agent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        agent_id = _parse_agent_id(d.pop("agent_id", UNSET))

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_locked_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        locked_at = _parse_locked_at(d.pop("locked_at", UNSET))

        def _parse_task(data: object) -> None | Unset | WorkspaceOwnerTask:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                task_type_0 = WorkspaceOwnerTask.from_dict(data)

                return task_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WorkspaceOwnerTask, data)

        task = _parse_task(d.pop("task", UNSET))

        def _parse_session(data: object) -> None | Unset | WorkspaceOwnerSession:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                session_type_0 = WorkspaceOwnerSession.from_dict(data)

                return session_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WorkspaceOwnerSession, data)

        session = _parse_session(d.pop("session", UNSET))

        def _parse_sessions_matched(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        sessions_matched = _parse_sessions_matched(d.pop("sessions_matched", UNSET))

        def _parse_fence(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        fence = _parse_fence(d.pop("fence", UNSET))

        def _parse_session_matches_claim(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        session_matches_claim = _parse_session_matches_claim(d.pop("session_matches_claim", UNSET))

        workspace_owner = cls(
            lock_state=lock_state,
            facts=facts,
            classification=classification,
            agent_id=agent_id,
            task_id=task_id,
            locked_at=locked_at,
            task=task,
            session=session,
            sessions_matched=sessions_matched,
            fence=fence,
            session_matches_claim=session_matches_claim,
        )

        workspace_owner.additional_properties = d
        return workspace_owner

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
