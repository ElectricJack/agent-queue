from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="JobRetainArgs")


@_attrs_define
class JobRetainArgs:
    """Retain a completed capture under durable artifact identities.

    Bounded on purpose: the capture's per-view PNGs and its receipt are what a
    ``ScoreReceipt`` names, while the identity planes and the adapter's own
    transcript stay behind ``aq job logs`` unless they are asked for.

        Attributes:
            job_id (str):
            project_id (None | str | Unset):
            task_id (None | str | Unset):
            session_id (None | str | Unset):
            views (list[str] | Unset):
            include_channels (bool | Unset):  Default: False.
    """

    job_id: str
    project_id: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    session_id: None | str | Unset = UNSET
    views: list[str] | Unset = UNSET
    include_channels: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        job_id = self.job_id

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

        views: list[str] | Unset = UNSET
        if not isinstance(self.views, Unset):
            views = self.views

        include_channels = self.include_channels

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "job_id": job_id,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if session_id is not UNSET:
            field_dict["session_id"] = session_id
        if views is not UNSET:
            field_dict["views"] = views
        if include_channels is not UNSET:
            field_dict["include_channels"] = include_channels

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        job_id = d.pop("job_id")

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

        views = cast(list[str], d.pop("views", UNSET))

        include_channels = d.pop("include_channels", UNSET)

        job_retain_args = cls(
            job_id=job_id,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            views=views,
            include_channels=include_channels,
        )

        return job_retain_args
