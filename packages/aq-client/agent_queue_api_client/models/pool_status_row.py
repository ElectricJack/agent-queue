from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.pool_instance_status import PoolInstanceStatus
    from ..models.pool_project_status import PoolProjectStatus


T = TypeVar("T", bound="PoolStatusRow")


@_attrs_define
class PoolStatusRow:
    """One worker pool -- a profile, fleet-wide (global-worker-pools §6.1).

    A row used to be one ``(project_id, profile_id)`` pair, which quietly
    multiplied ``min_active``/``max_active`` by the number of active
    projects.  Bounds and supply are aggregates over the whole fleet now,
    and the per-project detail lives in ``projects``.

        Attributes:
            profile_id (str):
            min_active (int):
            desired (int):
            running_idle (int):
            running_busy (int):
            starting (int):
            draining (int):
            ready (int):
            enabled (bool | Unset):  Default: True.
            max_active (int | None | Unset):
            min_per_project (int | Unset):  Default: 0.
            projects (list[PoolProjectStatus] | Unset):
            instances (list[PoolInstanceStatus] | Unset):
    """

    profile_id: str
    min_active: int
    desired: int
    running_idle: int
    running_busy: int
    starting: int
    draining: int
    ready: int
    enabled: bool | Unset = True
    max_active: int | None | Unset = UNSET
    min_per_project: int | Unset = 0
    projects: list[PoolProjectStatus] | Unset = UNSET
    instances: list[PoolInstanceStatus] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        profile_id = self.profile_id

        min_active = self.min_active

        desired = self.desired

        running_idle = self.running_idle

        running_busy = self.running_busy

        starting = self.starting

        draining = self.draining

        ready = self.ready

        enabled = self.enabled

        max_active: int | None | Unset
        if isinstance(self.max_active, Unset):
            max_active = UNSET
        else:
            max_active = self.max_active

        min_per_project = self.min_per_project

        projects: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.projects, Unset):
            projects = []
            for projects_item_data in self.projects:
                projects_item = projects_item_data.to_dict()
                projects.append(projects_item)

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
        if enabled is not UNSET:
            field_dict["enabled"] = enabled
        if max_active is not UNSET:
            field_dict["max_active"] = max_active
        if min_per_project is not UNSET:
            field_dict["min_per_project"] = min_per_project
        if projects is not UNSET:
            field_dict["projects"] = projects
        if instances is not UNSET:
            field_dict["instances"] = instances

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.pool_instance_status import PoolInstanceStatus
        from ..models.pool_project_status import PoolProjectStatus

        d = dict(src_dict)
        profile_id = d.pop("profile_id")

        min_active = d.pop("min_active")

        desired = d.pop("desired")

        running_idle = d.pop("running_idle")

        running_busy = d.pop("running_busy")

        starting = d.pop("starting")

        draining = d.pop("draining")

        ready = d.pop("ready")

        enabled = d.pop("enabled", UNSET)

        def _parse_max_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_active = _parse_max_active(d.pop("max_active", UNSET))

        min_per_project = d.pop("min_per_project", UNSET)

        _projects = d.pop("projects", UNSET)
        projects: list[PoolProjectStatus] | Unset = UNSET
        if _projects is not UNSET:
            projects = []
            for projects_item_data in _projects:
                projects_item = PoolProjectStatus.from_dict(projects_item_data)

                projects.append(projects_item)

        _instances = d.pop("instances", UNSET)
        instances: list[PoolInstanceStatus] | Unset = UNSET
        if _instances is not UNSET:
            instances = []
            for instances_item_data in _instances:
                instances_item = PoolInstanceStatus.from_dict(instances_item_data)

                instances.append(instances_item)

        pool_status_row = cls(
            profile_id=profile_id,
            min_active=min_active,
            desired=desired,
            running_idle=running_idle,
            running_busy=running_busy,
            starting=starting,
            draining=draining,
            ready=ready,
            enabled=enabled,
            max_active=max_active,
            min_per_project=min_per_project,
            projects=projects,
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
