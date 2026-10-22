from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.wait_list_args_schema_version import WaitListArgsSchemaVersion
from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitListArgs")


@_attrs_define
class WaitListArgs:
    """
    Attributes:
        schema_version (WaitListArgsSchemaVersion | Unset):  Default: WaitListArgsSchemaVersion.VALUE_1.
        project_id (None | str | Unset):
        task_id (None | str | Unset):
        session_id (None | str | Unset):
        limit (int | Unset):  Default: 100.
        offset (int | Unset):  Default: 0.
    """

    schema_version: WaitListArgsSchemaVersion | Unset = WaitListArgsSchemaVersion.VALUE_1
    project_id: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    session_id: None | str | Unset = UNSET
    limit: int | Unset = 100
    offset: int | Unset = 0

    def to_dict(self) -> dict[str, Any]:
        schema_version: int | Unset = UNSET
        if not isinstance(self.schema_version, Unset):
            schema_version = self.schema_version.value

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        session_id: None | str | Unset
        if isinstance(self.session_id, Unset):
            session_id = UNSET
        else:
            session_id = self.session_id

        limit = self.limit

        offset = self.offset

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if session_id is not UNSET:
            field_dict["session_id"] = session_id
        if limit is not UNSET:
            field_dict["limit"] = limit
        if offset is not UNSET:
            field_dict["offset"] = offset

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        _schema_version = d.pop("schema_version", UNSET)
        schema_version: WaitListArgsSchemaVersion | Unset
        if isinstance(_schema_version, Unset):
            schema_version = UNSET
        else:
            schema_version = WaitListArgsSchemaVersion(_schema_version)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_session_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        session_id = _parse_session_id(d.pop("session_id", UNSET))

        limit = d.pop("limit", UNSET)

        offset = d.pop("offset", UNSET)

        wait_list_args = cls(
            schema_version=schema_version,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            limit=limit,
            offset=offset,
        )

        return wait_list_args
