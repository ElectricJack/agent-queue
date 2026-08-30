from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SessionSummary")


@_attrs_define
class SessionSummary:
    """One row of ``session_list`` output.

    ``idle_seconds`` and ``stalled`` are derived per-row in
    ``_cmd_session_list``; every other field mirrors ``sessions`` table
    columns via ``SessionCommandsMixin._session_dict``.

        Attributes:
            id (str):
            name (str):
            task_id (None | str | Unset):
            project_id (None | str | Unset):
            profile_id (None | str | Unset):
            harness (None | str | Unset):
            provider (None | str | Unset):
            lifecycle (None | str | Unset):
            state (None | str | Unset):
            work_dir (None | str | Unset):
            started_at (float | None | Unset):
            last_activity (float | None | Unset):
            restarts (int | Unset):  Default: 0.
            quarantined_at (float | None | Unset):
            sleep_reason (None | str | Unset):
            epoch (None | str | Unset):
            idle_seconds (float | Unset):  Default: 0.0.
            stalled (bool | Unset):  Default: False.
    """

    id: str
    name: str
    task_id: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    profile_id: None | str | Unset = UNSET
    harness: None | str | Unset = UNSET
    provider: None | str | Unset = UNSET
    lifecycle: None | str | Unset = UNSET
    state: None | str | Unset = UNSET
    work_dir: None | str | Unset = UNSET
    started_at: float | None | Unset = UNSET
    last_activity: float | None | Unset = UNSET
    restarts: int | Unset = 0
    quarantined_at: float | None | Unset = UNSET
    sleep_reason: None | str | Unset = UNSET
    epoch: None | str | Unset = UNSET
    idle_seconds: float | Unset = 0.0
    stalled: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        name = self.name

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        harness: None | str | Unset
        if isinstance(self.harness, Unset):
            harness = UNSET
        else:
            harness = self.harness

        provider: None | str | Unset
        if isinstance(self.provider, Unset):
            provider = UNSET
        else:
            provider = self.provider

        lifecycle: None | str | Unset
        if isinstance(self.lifecycle, Unset):
            lifecycle = UNSET
        else:
            lifecycle = self.lifecycle

        state: None | str | Unset
        if isinstance(self.state, Unset):
            state = UNSET
        else:
            state = self.state

        work_dir: None | str | Unset
        if isinstance(self.work_dir, Unset):
            work_dir = UNSET
        else:
            work_dir = self.work_dir

        started_at: float | None | Unset
        if isinstance(self.started_at, Unset):
            started_at = UNSET
        else:
            started_at = self.started_at

        last_activity: float | None | Unset
        if isinstance(self.last_activity, Unset):
            last_activity = UNSET
        else:
            last_activity = self.last_activity

        restarts = self.restarts

        quarantined_at: float | None | Unset
        if isinstance(self.quarantined_at, Unset):
            quarantined_at = UNSET
        else:
            quarantined_at = self.quarantined_at

        sleep_reason: None | str | Unset
        if isinstance(self.sleep_reason, Unset):
            sleep_reason = UNSET
        else:
            sleep_reason = self.sleep_reason

        epoch: None | str | Unset
        if isinstance(self.epoch, Unset):
            epoch = UNSET
        else:
            epoch = self.epoch

        idle_seconds = self.idle_seconds

        stalled = self.stalled

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "name": name,
            }
        )
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if harness is not UNSET:
            field_dict["harness"] = harness
        if provider is not UNSET:
            field_dict["provider"] = provider
        if lifecycle is not UNSET:
            field_dict["lifecycle"] = lifecycle
        if state is not UNSET:
            field_dict["state"] = state
        if work_dir is not UNSET:
            field_dict["work_dir"] = work_dir
        if started_at is not UNSET:
            field_dict["started_at"] = started_at
        if last_activity is not UNSET:
            field_dict["last_activity"] = last_activity
        if restarts is not UNSET:
            field_dict["restarts"] = restarts
        if quarantined_at is not UNSET:
            field_dict["quarantined_at"] = quarantined_at
        if sleep_reason is not UNSET:
            field_dict["sleep_reason"] = sleep_reason
        if epoch is not UNSET:
            field_dict["epoch"] = epoch
        if idle_seconds is not UNSET:
            field_dict["idle_seconds"] = idle_seconds
        if stalled is not UNSET:
            field_dict["stalled"] = stalled

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))

        def _parse_harness(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        harness = _parse_harness(d.pop("harness", UNSET))

        def _parse_provider(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        provider = _parse_provider(d.pop("provider", UNSET))

        def _parse_lifecycle(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        lifecycle = _parse_lifecycle(d.pop("lifecycle", UNSET))

        def _parse_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        state = _parse_state(d.pop("state", UNSET))

        def _parse_work_dir(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        work_dir = _parse_work_dir(d.pop("work_dir", UNSET))

        def _parse_started_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        started_at = _parse_started_at(d.pop("started_at", UNSET))

        def _parse_last_activity(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        last_activity = _parse_last_activity(d.pop("last_activity", UNSET))

        restarts = d.pop("restarts", UNSET)

        def _parse_quarantined_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        quarantined_at = _parse_quarantined_at(d.pop("quarantined_at", UNSET))

        def _parse_sleep_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        sleep_reason = _parse_sleep_reason(d.pop("sleep_reason", UNSET))

        def _parse_epoch(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        epoch = _parse_epoch(d.pop("epoch", UNSET))

        idle_seconds = d.pop("idle_seconds", UNSET)

        stalled = d.pop("stalled", UNSET)

        session_summary = cls(
            id=id,
            name=name,
            task_id=task_id,
            project_id=project_id,
            profile_id=profile_id,
            harness=harness,
            provider=provider,
            lifecycle=lifecycle,
            state=state,
            work_dir=work_dir,
            started_at=started_at,
            last_activity=last_activity,
            restarts=restarts,
            quarantined_at=quarantined_at,
            sleep_reason=sleep_reason,
            epoch=epoch,
            idle_seconds=idle_seconds,
            stalled=stalled,
        )

        session_summary.additional_properties = d
        return session_summary

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
