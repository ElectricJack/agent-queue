from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PoolProjectStatus")


@_attrs_define
class PoolProjectStatus:
    """Where one pool's workers actually are, in a single project.

    Sizing is fleet-wide, but a worker still lives in one project for its
    lifetime, so "where are my workers running" has to stay answerable.
    These are the placement inputs and the per-project share of the pool's
    supply, for the one project named by ``project_id``.

        Attributes:
            project_id (str):
            ready (int | Unset):  Default: 0.
            running_idle (int | Unset):  Default: 0.
            running_busy (int | Unset):  Default: 0.
            starting (int | Unset):  Default: 0.
            draining (int | Unset):  Default: 0.
            max_concurrent_agents (int | None | Unset):
            workspace_capacity (int | Unset):  Default: 0.
            quarantined_until (float | None | Unset):
            quarantined_reason (None | str | Unset):
    """

    project_id: str
    ready: int | Unset = 0
    running_idle: int | Unset = 0
    running_busy: int | Unset = 0
    starting: int | Unset = 0
    draining: int | Unset = 0
    max_concurrent_agents: int | None | Unset = UNSET
    workspace_capacity: int | Unset = 0
    quarantined_until: float | None | Unset = UNSET
    quarantined_reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        ready = self.ready

        running_idle = self.running_idle

        running_busy = self.running_busy

        starting = self.starting

        draining = self.draining

        max_concurrent_agents: int | None | Unset
        if isinstance(self.max_concurrent_agents, Unset):
            max_concurrent_agents = UNSET
        else:
            max_concurrent_agents = self.max_concurrent_agents

        workspace_capacity = self.workspace_capacity

        quarantined_until: float | None | Unset
        if isinstance(self.quarantined_until, Unset):
            quarantined_until = UNSET
        else:
            quarantined_until = self.quarantined_until

        quarantined_reason: None | str | Unset
        if isinstance(self.quarantined_reason, Unset):
            quarantined_reason = UNSET
        else:
            quarantined_reason = self.quarantined_reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if ready is not UNSET:
            field_dict["ready"] = ready
        if running_idle is not UNSET:
            field_dict["running_idle"] = running_idle
        if running_busy is not UNSET:
            field_dict["running_busy"] = running_busy
        if starting is not UNSET:
            field_dict["starting"] = starting
        if draining is not UNSET:
            field_dict["draining"] = draining
        if max_concurrent_agents is not UNSET:
            field_dict["max_concurrent_agents"] = max_concurrent_agents
        if workspace_capacity is not UNSET:
            field_dict["workspace_capacity"] = workspace_capacity
        if quarantined_until is not UNSET:
            field_dict["quarantined_until"] = quarantined_until
        if quarantined_reason is not UNSET:
            field_dict["quarantined_reason"] = quarantined_reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        ready = d.pop("ready", UNSET)

        running_idle = d.pop("running_idle", UNSET)

        running_busy = d.pop("running_busy", UNSET)

        starting = d.pop("starting", UNSET)

        draining = d.pop("draining", UNSET)

        def _parse_max_concurrent_agents(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_concurrent_agents = _parse_max_concurrent_agents(d.pop("max_concurrent_agents", UNSET))

        workspace_capacity = d.pop("workspace_capacity", UNSET)

        def _parse_quarantined_until(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        quarantined_until = _parse_quarantined_until(d.pop("quarantined_until", UNSET))

        def _parse_quarantined_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        quarantined_reason = _parse_quarantined_reason(d.pop("quarantined_reason", UNSET))

        pool_project_status = cls(
            project_id=project_id,
            ready=ready,
            running_idle=running_idle,
            running_busy=running_busy,
            starting=starting,
            draining=draining,
            max_concurrent_agents=max_concurrent_agents,
            workspace_capacity=workspace_capacity,
            quarantined_until=quarantined_until,
            quarantined_reason=quarantined_reason,
        )

        pool_project_status.additional_properties = d
        return pool_project_status

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
