from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.schedule_list_args_state_type_0 import ScheduleListArgsStateType0
from ..types import UNSET, Unset

T = TypeVar("T", bound="ScheduleListArgs")


@_attrs_define
class ScheduleListArgs:
    """
    Attributes:
        project_id (str):
        state (None | ScheduleListArgsStateType0 | Unset):
        limit (int | Unset):  Default: 100.
        offset (int | Unset):  Default: 0.
    """

    project_id: str
    state: None | ScheduleListArgsStateType0 | Unset = UNSET
    limit: int | Unset = 100
    offset: int | Unset = 0

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        state: None | str | Unset
        if isinstance(self.state, Unset):
            state = UNSET
        elif isinstance(self.state, ScheduleListArgsStateType0):
            state = self.state.value
        else:
            state = self.state

        limit = self.limit

        offset = self.offset

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if state is not UNSET:
            field_dict["state"] = state
        if limit is not UNSET:
            field_dict["limit"] = limit
        if offset is not UNSET:
            field_dict["offset"] = offset

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        def _parse_state(data: object) -> None | ScheduleListArgsStateType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                state_type_0 = ScheduleListArgsStateType0(data)

                return state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ScheduleListArgsStateType0 | Unset, data)

        state = _parse_state(d.pop("state", UNSET))

        limit = d.pop("limit", UNSET)

        offset = d.pop("offset", UNSET)

        schedule_list_args = cls(
            project_id=project_id,
            state=state,
            limit=limit,
            offset=offset,
        )

        return schedule_list_args
