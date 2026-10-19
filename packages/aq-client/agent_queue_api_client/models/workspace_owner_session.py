from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WorkspaceOwnerSession")


@_attrs_define
class WorkspaceOwnerSession:
    """
    Attributes:
        id (None | str | Unset):
        agent_id (None | str | Unset):
        task_id (None | str | Unset):
        lifecycle (None | str | Unset):
        state (None | str | Unset):
        claim_phase (None | str | Unset):
        last_claim_epoch (int | None | Unset):
        last_activity (float | None | Unset):
        work_dir (None | str | Unset):
    """

    id: None | str | Unset = UNSET
    agent_id: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    lifecycle: None | str | Unset = UNSET
    state: None | str | Unset = UNSET
    claim_phase: None | str | Unset = UNSET
    last_claim_epoch: int | None | Unset = UNSET
    last_activity: float | None | Unset = UNSET
    work_dir: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id: None | str | Unset
        if isinstance(self.id, Unset):
            id = UNSET
        else:
            id = self.id

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

        lifecycle: None | str | Unset
        if isinstance(self.lifecycle, Unset):
            lifecycle = UNSET
        else:
            lifecycle = self.lifecycle

        state: None | str | Unset
        if isinstance(self.state, Unset):
            state = UNSET
        else:
            state = self.state

        claim_phase: None | str | Unset
        if isinstance(self.claim_phase, Unset):
            claim_phase = UNSET
        else:
            claim_phase = self.claim_phase

        last_claim_epoch: int | None | Unset
        if isinstance(self.last_claim_epoch, Unset):
            last_claim_epoch = UNSET
        else:
            last_claim_epoch = self.last_claim_epoch

        last_activity: float | None | Unset
        if isinstance(self.last_activity, Unset):
            last_activity = UNSET
        else:
            last_activity = self.last_activity

        work_dir: None | str | Unset
        if isinstance(self.work_dir, Unset):
            work_dir = UNSET
        else:
            work_dir = self.work_dir

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if id is not UNSET:
            field_dict["id"] = id
        if agent_id is not UNSET:
            field_dict["agent_id"] = agent_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if lifecycle is not UNSET:
            field_dict["lifecycle"] = lifecycle
        if state is not UNSET:
            field_dict["state"] = state
        if claim_phase is not UNSET:
            field_dict["claim_phase"] = claim_phase
        if last_claim_epoch is not UNSET:
            field_dict["last_claim_epoch"] = last_claim_epoch
        if last_activity is not UNSET:
            field_dict["last_activity"] = last_activity
        if work_dir is not UNSET:
            field_dict["work_dir"] = work_dir

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        id = _parse_id(d.pop("id", UNSET))

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

        def _parse_lifecycle(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        lifecycle = _parse_lifecycle(d.pop("lifecycle", UNSET))

        def _parse_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        state = _parse_state(d.pop("state", UNSET))

        def _parse_claim_phase(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        claim_phase = _parse_claim_phase(d.pop("claim_phase", UNSET))

        def _parse_last_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        last_claim_epoch = _parse_last_claim_epoch(d.pop("last_claim_epoch", UNSET))

        def _parse_last_activity(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        last_activity = _parse_last_activity(d.pop("last_activity", UNSET))

        def _parse_work_dir(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        work_dir = _parse_work_dir(d.pop("work_dir", UNSET))

        workspace_owner_session = cls(
            id=id,
            agent_id=agent_id,
            task_id=task_id,
            lifecycle=lifecycle,
            state=state,
            claim_phase=claim_phase,
            last_claim_epoch=last_claim_epoch,
            last_activity=last_activity,
            work_dir=work_dir,
        )

        workspace_owner_session.additional_properties = d
        return workspace_owner_session

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
