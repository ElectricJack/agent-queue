from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.pool_instance_status import PoolInstanceStatus


T = TypeVar("T", bound="PoolStatusRow")


@_attrs_define
class PoolStatusRow:
    """One (project, profile) worker-pool row — swarm-work-model §11.

    Attributes:
        project_id (str):
        profile_id (str):
        min_active (int):
        desired (int):
        running_idle (int):
        running_busy (int):
        starting (int):
        draining (int):
        ready (int):
        max_active (int | None | Unset):
        quarantined_until (float | None | Unset):
        quarantined_reason (None | str | Unset):
        instances (list[PoolInstanceStatus] | Unset):
    """

    project_id: str
    profile_id: str
    min_active: int
    desired: int
    running_idle: int
    running_busy: int
    starting: int
    draining: int
    ready: int
    max_active: int | None | Unset = UNSET
    quarantined_until: float | None | Unset = UNSET
    quarantined_reason: None | str | Unset = UNSET
    instances: list[PoolInstanceStatus] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        profile_id = self.profile_id

        min_active = self.min_active

        desired = self.desired

        running_idle = self.running_idle

        running_busy = self.running_busy

        starting = self.starting

        draining = self.draining

        ready = self.ready

        max_active: int | None | Unset
        if isinstance(self.max_active, Unset):
            max_active = UNSET
        else:
            max_active = self.max_active

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

        instances: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.instances, Unset):
            instances = []
            for instances_item_data in self.instances:
                instances_item = instances_item_data.to_dict()
                instances.append(instances_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "profile_id": profile_id,
                "min_active": min_active,
                "desired": desired,
                "running_idle": running_idle,
                "running_busy": running_busy,
                "starting": starting,
                "draining": draining,
                "ready": ready,
            }
        )
        if max_active is not UNSET:
            field_dict["max_active"] = max_active
        if quarantined_until is not UNSET:
            field_dict["quarantined_until"] = quarantined_until
        if quarantined_reason is not UNSET:
            field_dict["quarantined_reason"] = quarantined_reason
        if instances is not UNSET:
            field_dict["instances"] = instances

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.pool_instance_status import PoolInstanceStatus

        d = dict(src_dict)
        project_id = d.pop("project_id")

        profile_id = d.pop("profile_id")

        min_active = d.pop("min_active")

        desired = d.pop("desired")

        running_idle = d.pop("running_idle")

        running_busy = d.pop("running_busy")

        starting = d.pop("starting")

        draining = d.pop("draining")

        ready = d.pop("ready")

        def _parse_max_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_active = _parse_max_active(d.pop("max_active", UNSET))

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

        _instances = d.pop("instances", UNSET)
        instances: list[PoolInstanceStatus] | Unset = UNSET
        if _instances is not UNSET:
            instances = []
            for instances_item_data in _instances:
                instances_item = PoolInstanceStatus.from_dict(instances_item_data)

                instances.append(instances_item)

        pool_status_row = cls(
            project_id=project_id,
            profile_id=profile_id,
            min_active=min_active,
            desired=desired,
            running_idle=running_idle,
            running_busy=running_busy,
            starting=starting,
            draining=draining,
            ready=ready,
            max_active=max_active,
            quarantined_until=quarantined_until,
            quarantined_reason=quarantined_reason,
            instances=instances,
        )

        pool_status_row.additional_properties = d
        return pool_status_row

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
