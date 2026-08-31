from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_run_path_entry import PlaybookRunPathEntry


T = TypeVar("T", bound="PlaybookRunSummary")


@_attrs_define
class PlaybookRunSummary:
    """
    Attributes:
        run_id (str):
        playbook_id (str):
        playbook_version (int):
        status (str):
        current_node (None | str | Unset):
        tokens_used (int | Unset):  Default: 0.
        started_at (float | None | Unset):
        completed_at (float | None | Unset):
        path (list[PlaybookRunPathEntry] | Unset):
        duration_seconds (float | None | Unset):
        error (None | str | Unset):
    """

    run_id: str
    playbook_id: str
    playbook_version: int
    status: str
    current_node: None | str | Unset = UNSET
    tokens_used: int | Unset = 0
    started_at: float | None | Unset = UNSET
    completed_at: float | None | Unset = UNSET
    path: list[PlaybookRunPathEntry] | Unset = UNSET
    duration_seconds: float | None | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        run_id = self.run_id

        playbook_id = self.playbook_id

        playbook_version = self.playbook_version

        status = self.status

        current_node: None | str | Unset
        if isinstance(self.current_node, Unset):
            current_node = UNSET
        else:
            current_node = self.current_node

        tokens_used = self.tokens_used

        started_at: float | None | Unset
        if isinstance(self.started_at, Unset):
            started_at = UNSET
        else:
            started_at = self.started_at

        completed_at: float | None | Unset
        if isinstance(self.completed_at, Unset):
            completed_at = UNSET
        else:
            completed_at = self.completed_at

        path: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.path, Unset):
            path = []
            for path_item_data in self.path:
                path_item = path_item_data.to_dict()
                path.append(path_item)

        duration_seconds: float | None | Unset
        if isinstance(self.duration_seconds, Unset):
            duration_seconds = UNSET
        else:
            duration_seconds = self.duration_seconds

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "run_id": run_id,
                "playbook_id": playbook_id,
                "playbook_version": playbook_version,
                "status": status,
            }
        )
        if current_node is not UNSET:
            field_dict["current_node"] = current_node
        if tokens_used is not UNSET:
            field_dict["tokens_used"] = tokens_used
        if started_at is not UNSET:
            field_dict["started_at"] = started_at
        if completed_at is not UNSET:
            field_dict["completed_at"] = completed_at
        if path is not UNSET:
            field_dict["path"] = path
        if duration_seconds is not UNSET:
            field_dict["duration_seconds"] = duration_seconds
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_run_path_entry import PlaybookRunPathEntry  # noqa: PLC0415

        d = dict(src_dict)
        run_id = d.pop("run_id")

        playbook_id = d.pop("playbook_id")

        playbook_version = d.pop("playbook_version")

        status = d.pop("status")

        def _parse_current_node(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_node = _parse_current_node(d.pop("current_node", UNSET))

        tokens_used = d.pop("tokens_used", UNSET)

        def _parse_started_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        started_at = _parse_started_at(d.pop("started_at", UNSET))

        def _parse_completed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        completed_at = _parse_completed_at(d.pop("completed_at", UNSET))

        _path = d.pop("path", UNSET)
        path: list[PlaybookRunPathEntry] | Unset = UNSET
        if _path is not UNSET:
            path = []
            for path_item_data in _path:
                path_item = PlaybookRunPathEntry.from_dict(path_item_data)

                path.append(path_item)

        def _parse_duration_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        duration_seconds = _parse_duration_seconds(d.pop("duration_seconds", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        playbook_run_summary = cls(
            run_id=run_id,
            playbook_id=playbook_id,
            playbook_version=playbook_version,
            status=status,
            current_node=current_node,
            tokens_used=tokens_used,
            started_at=started_at,
            completed_at=completed_at,
            path=path,
            duration_seconds=duration_seconds,
            error=error,
        )

        playbook_run_summary.additional_properties = d
        return playbook_run_summary

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
