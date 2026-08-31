from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.set_project_constraint_request_max_agents_by_type_type_0 import (
        SetProjectConstraintRequestMaxAgentsByTypeType0,
    )


T = TypeVar("T", bound="SetProjectConstraintRequest")


@_attrs_define
class SetProjectConstraintRequest:
    """
    Attributes:
        project_id (str): Project ID
        exclusive (bool | None | Unset): If true, only one agent may work on the project at a time (overrides
            max_concurrent_agents to 1).
        max_agents_by_type (None | SetProjectConstraintRequestMaxAgentsByTypeType0 | Unset): Per-agent-type concurrency
            limits, e.g. {"claude": 2, "codex": 1}. Agent types not listed are unrestricted.
        pause_scheduling (bool | None | Unset): If true, the scheduler skips this project entirely — no new tasks are
            assigned until the constraint is released.
        created_by (None | str | Unset): Identifier of who/what set the constraint (e.g. workflow ID, admin name).
            Informational only.
    """

    project_id: str
    exclusive: bool | None | Unset = UNSET
    max_agents_by_type: None | SetProjectConstraintRequestMaxAgentsByTypeType0 | Unset = UNSET
    pause_scheduling: bool | None | Unset = UNSET
    created_by: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.set_project_constraint_request_max_agents_by_type_type_0 import (
            SetProjectConstraintRequestMaxAgentsByTypeType0,  # noqa: PLC0415
        )

        project_id = self.project_id

        exclusive: bool | None | Unset
        if isinstance(self.exclusive, Unset):
            exclusive = UNSET
        else:
            exclusive = self.exclusive

        max_agents_by_type: dict[str, Any] | None | Unset
        if isinstance(self.max_agents_by_type, Unset):
            max_agents_by_type = UNSET
        elif isinstance(self.max_agents_by_type, SetProjectConstraintRequestMaxAgentsByTypeType0):
            max_agents_by_type = self.max_agents_by_type.to_dict()
        else:
            max_agents_by_type = self.max_agents_by_type

        pause_scheduling: bool | None | Unset
        if isinstance(self.pause_scheduling, Unset):
            pause_scheduling = UNSET
        else:
            pause_scheduling = self.pause_scheduling

        created_by: None | str | Unset
        if isinstance(self.created_by, Unset):
            created_by = UNSET
        else:
            created_by = self.created_by

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if exclusive is not UNSET:
            field_dict["exclusive"] = exclusive
        if max_agents_by_type is not UNSET:
            field_dict["max_agents_by_type"] = max_agents_by_type
        if pause_scheduling is not UNSET:
            field_dict["pause_scheduling"] = pause_scheduling
        if created_by is not UNSET:
            field_dict["created_by"] = created_by

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.set_project_constraint_request_max_agents_by_type_type_0 import (
            SetProjectConstraintRequestMaxAgentsByTypeType0,  # noqa: PLC0415
        )

        d = dict(src_dict)
        project_id = d.pop("project_id")

        def _parse_exclusive(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        exclusive = _parse_exclusive(d.pop("exclusive", UNSET))

        def _parse_max_agents_by_type(data: object) -> None | SetProjectConstraintRequestMaxAgentsByTypeType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                max_agents_by_type_type_0 = SetProjectConstraintRequestMaxAgentsByTypeType0.from_dict(data)

                return max_agents_by_type_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | SetProjectConstraintRequestMaxAgentsByTypeType0 | Unset, data)

        max_agents_by_type = _parse_max_agents_by_type(d.pop("max_agents_by_type", UNSET))

        def _parse_pause_scheduling(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        pause_scheduling = _parse_pause_scheduling(d.pop("pause_scheduling", UNSET))

        def _parse_created_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        created_by = _parse_created_by(d.pop("created_by", UNSET))

        set_project_constraint_request = cls(
            project_id=project_id,
            exclusive=exclusive,
            max_agents_by_type=max_agents_by_type,
            pause_scheduling=pause_scheduling,
            created_by=created_by,
        )

        set_project_constraint_request.additional_properties = d
        return set_project_constraint_request

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
