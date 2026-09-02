from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="LayoutJob")


@_attrs_define
class LayoutJob:
    """
    Attributes:
        id (str):
        project_id (str):
        variant (str):
        kind (str):
        status (str):
        requested_at (float):
        started_at (float | None | Unset):
        finished_at (float | None | Unset):
        error (None | str | Unset):
    """

    id: str
    project_id: str
    variant: str
    kind: str
    status: str
    requested_at: float
    started_at: float | None | Unset = UNSET
    finished_at: float | None | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        project_id = self.project_id

        variant = self.variant

        kind = self.kind

        status = self.status

        requested_at = self.requested_at

        started_at: float | None | Unset
        if isinstance(self.started_at, Unset):
            started_at = UNSET
        else:
            started_at = self.started_at

        finished_at: float | None | Unset
        if isinstance(self.finished_at, Unset):
            finished_at = UNSET
        else:
            finished_at = self.finished_at

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "project_id": project_id,
                "variant": variant,
                "kind": kind,
                "status": status,
                "requested_at": requested_at,
            }
        )
        if started_at is not UNSET:
            field_dict["started_at"] = started_at
        if finished_at is not UNSET:
            field_dict["finished_at"] = finished_at
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        project_id = d.pop("project_id")

        variant = d.pop("variant")

        kind = d.pop("kind")

        status = d.pop("status")

        requested_at = d.pop("requested_at")

        def _parse_started_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        started_at = _parse_started_at(d.pop("started_at", UNSET))

        def _parse_finished_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        finished_at = _parse_finished_at(d.pop("finished_at", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        layout_job = cls(
            id=id,
            project_id=project_id,
            variant=variant,
            kind=kind,
            status=status,
            requested_at=requested_at,
            started_at=started_at,
            finished_at=finished_at,
            error=error,
        )

        layout_job.additional_properties = d
        return layout_job

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
