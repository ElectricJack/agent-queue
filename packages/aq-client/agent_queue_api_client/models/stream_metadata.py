from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="StreamMetadata")


@_attrs_define
class StreamMetadata:
    """
    Attributes:
        stream_id (str):
        title (str):
        status (str):
        exit_code (int | None):
        started_at (float):
        ended_at (float | None):
        session_id (str):
        project_id (None | str):
    """

    stream_id: str
    title: str
    status: str
    exit_code: int | None
    started_at: float
    ended_at: float | None
    session_id: str
    project_id: None | str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        stream_id = self.stream_id

        title = self.title

        status = self.status

        exit_code: int | None
        exit_code = self.exit_code

        started_at = self.started_at

        ended_at: float | None
        ended_at = self.ended_at

        session_id = self.session_id

        project_id: None | str
        project_id = self.project_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "stream_id": stream_id,
                "title": title,
                "status": status,
                "exit_code": exit_code,
                "started_at": started_at,
                "ended_at": ended_at,
                "session_id": session_id,
                "project_id": project_id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        stream_id = d.pop("stream_id")

        title = d.pop("title")

        status = d.pop("status")

        def _parse_exit_code(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        exit_code = _parse_exit_code(d.pop("exit_code"))

        started_at = d.pop("started_at")

        def _parse_ended_at(data: object) -> float | None:
            if data is None:
                return data
            return cast(float | None, data)

        ended_at = _parse_ended_at(d.pop("ended_at"))

        session_id = d.pop("session_id")

        def _parse_project_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        project_id = _parse_project_id(d.pop("project_id"))

        stream_metadata = cls(
            stream_id=stream_id,
            title=title,
            status=status,
            exit_code=exit_code,
            started_at=started_at,
            ended_at=ended_at,
            session_id=session_id,
            project_id=project_id,
        )

        stream_metadata.additional_properties = d
        return stream_metadata

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
