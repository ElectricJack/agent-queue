from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GateSummary")


@_attrs_define
class GateSummary:
    """Gate row as returned by ``gate_list`` / ``gate_show``.

    Fields come from ``src/database/queries/gate_queries.py`` which returns
    dict rows including the resolved metadata columns.

        Attributes:
            id (str):
            gate_type (str):
            project_id (str):
            title (str):
            question (str | Unset):  Default: ''.
            status (str | Unset):  Default: 'open'.
            await_id (None | str | Unset):
            timeout_at (float | None | Unset):
            created_at (float | None | Unset):
            resolved_at (float | None | Unset):
            resolved_by (None | str | Unset):
            resolution (None | str | Unset):
    """

    id: str
    gate_type: str
    project_id: str
    title: str
    question: str | Unset = ""
    status: str | Unset = "open"
    await_id: None | str | Unset = UNSET
    timeout_at: float | None | Unset = UNSET
    created_at: float | None | Unset = UNSET
    resolved_at: float | None | Unset = UNSET
    resolved_by: None | str | Unset = UNSET
    resolution: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        gate_type = self.gate_type

        project_id = self.project_id

        title = self.title

        question = self.question

        status = self.status

        await_id: None | str | Unset
        if isinstance(self.await_id, Unset):
            await_id = UNSET
        else:
            await_id = self.await_id

        timeout_at: float | None | Unset
        if isinstance(self.timeout_at, Unset):
            timeout_at = UNSET
        else:
            timeout_at = self.timeout_at

        created_at: float | None | Unset
        if isinstance(self.created_at, Unset):
            created_at = UNSET
        else:
            created_at = self.created_at

        resolved_at: float | None | Unset
        if isinstance(self.resolved_at, Unset):
            resolved_at = UNSET
        else:
            resolved_at = self.resolved_at

        resolved_by: None | str | Unset
        if isinstance(self.resolved_by, Unset):
            resolved_by = UNSET
        else:
            resolved_by = self.resolved_by

        resolution: None | str | Unset
        if isinstance(self.resolution, Unset):
            resolution = UNSET
        else:
            resolution = self.resolution

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "gate_type": gate_type,
                "project_id": project_id,
                "title": title,
            }
        )
        if question is not UNSET:
            field_dict["question"] = question
        if status is not UNSET:
            field_dict["status"] = status
        if await_id is not UNSET:
            field_dict["await_id"] = await_id
        if timeout_at is not UNSET:
            field_dict["timeout_at"] = timeout_at
        if created_at is not UNSET:
            field_dict["created_at"] = created_at
        if resolved_at is not UNSET:
            field_dict["resolved_at"] = resolved_at
        if resolved_by is not UNSET:
            field_dict["resolved_by"] = resolved_by
        if resolution is not UNSET:
            field_dict["resolution"] = resolution

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        gate_type = d.pop("gate_type")

        project_id = d.pop("project_id")

        title = d.pop("title")

        question = d.pop("question", UNSET)

        status = d.pop("status", UNSET)

        def _parse_await_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        await_id = _parse_await_id(d.pop("await_id", UNSET))

        def _parse_timeout_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        timeout_at = _parse_timeout_at(d.pop("timeout_at", UNSET))

        def _parse_created_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        created_at = _parse_created_at(d.pop("created_at", UNSET))

        def _parse_resolved_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        resolved_at = _parse_resolved_at(d.pop("resolved_at", UNSET))

        def _parse_resolved_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        resolved_by = _parse_resolved_by(d.pop("resolved_by", UNSET))

        def _parse_resolution(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        resolution = _parse_resolution(d.pop("resolution", UNSET))

        gate_summary = cls(
            id=id,
            gate_type=gate_type,
            project_id=project_id,
            title=title,
            question=question,
            status=status,
            await_id=await_id,
            timeout_at=timeout_at,
            created_at=created_at,
            resolved_at=resolved_at,
            resolved_by=resolved_by,
            resolution=resolution,
        )

        gate_summary.additional_properties = d
        return gate_summary

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
