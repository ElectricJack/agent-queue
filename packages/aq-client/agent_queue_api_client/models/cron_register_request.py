from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CronRegisterRequest")


@_attrs_define
class CronRegisterRequest:
    """
    Attributes:
        prompt (str):
        idempotency_key (str):
        session_id (None | str | Unset):
        project_id (None | str | Unset):
        task_id (None | str | Unset):
        claim_epoch (int | None | Unset):
        every (float | None | Unset):
        offset (float | Unset):  Default: 0.0.
        cron (None | str | Unset): MINUTE HOUR * * *: minute 0–59, * or */N; hour 0–23 or *.
        timezone (str | Unset):  Default: 'UTC'.
    """

    prompt: str
    idempotency_key: str
    session_id: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET
    every: float | None | Unset = UNSET
    offset: float | Unset = 0.0
    cron: None | str | Unset = UNSET
    timezone: str | Unset = "UTC"
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        prompt = self.prompt

        idempotency_key = self.idempotency_key

        session_id: None | str | Unset
        if isinstance(self.session_id, Unset):
            session_id = UNSET
        else:
            session_id = self.session_id

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

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        every: float | None | Unset
        if isinstance(self.every, Unset):
            every = UNSET
        else:
            every = self.every

        offset = self.offset

        cron: None | str | Unset
        if isinstance(self.cron, Unset):
            cron = UNSET
        else:
            cron = self.cron

        timezone = self.timezone

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "prompt": prompt,
                "idempotency_key": idempotency_key,
            }
        )
        if session_id is not UNSET:
            field_dict["session_id"] = session_id
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch
        if every is not UNSET:
            field_dict["every"] = every
        if offset is not UNSET:
            field_dict["offset"] = offset
        if cron is not UNSET:
            field_dict["cron"] = cron
        if timezone is not UNSET:
            field_dict["timezone"] = timezone

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        prompt = d.pop("prompt")

        idempotency_key = d.pop("idempotency_key")

        def _parse_session_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        session_id = _parse_session_id(d.pop("session_id", UNSET))

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

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        def _parse_every(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        every = _parse_every(d.pop("every", UNSET))

        offset = d.pop("offset", UNSET)

        def _parse_cron(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        cron = _parse_cron(d.pop("cron", UNSET))

        timezone = d.pop("timezone", UNSET)

        cron_register_request = cls(
            prompt=prompt,
            idempotency_key=idempotency_key,
            session_id=session_id,
            project_id=project_id,
            task_id=task_id,
            claim_epoch=claim_epoch,
            every=every,
            offset=offset,
            cron=cron,
            timezone=timezone,
        )

        cron_register_request.additional_properties = d
        return cron_register_request

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
