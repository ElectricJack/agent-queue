from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.v1_run_summary_dto_options_item import V1RunSummaryDTOOptionsItem
from ..models.v1_run_summary_dto_ownership import V1RunSummaryDTOOwnership
from ..types import UNSET, Unset

T = TypeVar("T", bound="V1RunSummaryDTO")


@_attrs_define
class V1RunSummaryDTO:
    """One active V1 run, as the drain sees it.

    ``ownership`` is the field the whole drain turns on: ``live`` means a
    coroutine still owns the run and it can finish by itself, ``orphaned``
    means the row outlived the process that started it and only an operator
    write will ever clear it.  ``options`` is never empty — ``cancel`` is
    always available.

        Attributes:
            run_id (str):
            playbook_id (str):
            playbook_version (int):
            status (str):
            started_at (float):
            age_seconds (float):
            ownership (V1RunSummaryDTOOwnership):
            options (list[V1RunSummaryDTOOptionsItem]):
            current_node (None | str | Unset):
            paused_at (float | None | Unset):
            waiting_for_event (None | str | Unset):
            event_id (None | str | Unset):
            project_id (None | str | Unset):
    """

    run_id: str
    playbook_id: str
    playbook_version: int
    status: str
    started_at: float
    age_seconds: float
    ownership: V1RunSummaryDTOOwnership
    options: list[V1RunSummaryDTOOptionsItem]
    current_node: None | str | Unset = UNSET
    paused_at: float | None | Unset = UNSET
    waiting_for_event: None | str | Unset = UNSET
    event_id: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        run_id = self.run_id

        playbook_id = self.playbook_id

        playbook_version = self.playbook_version

        status = self.status

        started_at = self.started_at

        age_seconds = self.age_seconds

        ownership = self.ownership.value

        options = []
        for options_item_data in self.options:
            options_item = options_item_data.value
            options.append(options_item)

        current_node: None | str | Unset
        if isinstance(self.current_node, Unset):
            current_node = UNSET
        else:
            current_node = self.current_node

        paused_at: float | None | Unset
        if isinstance(self.paused_at, Unset):
            paused_at = UNSET
        else:
            paused_at = self.paused_at

        waiting_for_event: None | str | Unset
        if isinstance(self.waiting_for_event, Unset):
            waiting_for_event = UNSET
        else:
            waiting_for_event = self.waiting_for_event

        event_id: None | str | Unset
        if isinstance(self.event_id, Unset):
            event_id = UNSET
        else:
            event_id = self.event_id

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "run_id": run_id,
                "playbook_id": playbook_id,
                "playbook_version": playbook_version,
                "status": status,
                "started_at": started_at,
                "age_seconds": age_seconds,
                "ownership": ownership,
                "options": options,
            }
        )
        if current_node is not UNSET:
            field_dict["current_node"] = current_node
        if paused_at is not UNSET:
            field_dict["paused_at"] = paused_at
        if waiting_for_event is not UNSET:
            field_dict["waiting_for_event"] = waiting_for_event
        if event_id is not UNSET:
            field_dict["event_id"] = event_id
        if project_id is not UNSET:
            field_dict["project_id"] = project_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        run_id = d.pop("run_id")

        playbook_id = d.pop("playbook_id")

        playbook_version = d.pop("playbook_version")

        status = d.pop("status")

        started_at = d.pop("started_at")

        age_seconds = d.pop("age_seconds")

        ownership = V1RunSummaryDTOOwnership(d.pop("ownership"))

        options = []
        _options = d.pop("options")
        for options_item_data in _options:
            options_item = V1RunSummaryDTOOptionsItem(options_item_data)

            options.append(options_item)

        def _parse_current_node(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_node = _parse_current_node(d.pop("current_node", UNSET))

        def _parse_paused_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        paused_at = _parse_paused_at(d.pop("paused_at", UNSET))

        def _parse_waiting_for_event(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        waiting_for_event = _parse_waiting_for_event(d.pop("waiting_for_event", UNSET))

        def _parse_event_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        event_id = _parse_event_id(d.pop("event_id", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        v1_run_summary_dto = cls(
            run_id=run_id,
            playbook_id=playbook_id,
            playbook_version=playbook_version,
            status=status,
            started_at=started_at,
            age_seconds=age_seconds,
            ownership=ownership,
            options=options,
            current_node=current_node,
            paused_at=paused_at,
            waiting_for_event=waiting_for_event,
            event_id=event_id,
            project_id=project_id,
        )

        return v1_run_summary_dto
