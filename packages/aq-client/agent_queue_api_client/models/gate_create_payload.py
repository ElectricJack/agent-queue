from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GateCreatePayload")


@_attrs_define
class GateCreatePayload:
    """Echoed by ``gate_create`` — matches the ``gate.created`` event payload.

    Attributes:
        gate_id (str):
        gate_type (str):
        project_id (str):
        title (str):
        question (str | Unset):  Default: ''.
        await_id (None | str | Unset):
        timeout_at (float | None | Unset):
        waiter_task_ids (list[str] | Unset):
    """

    gate_id: str
    gate_type: str
    project_id: str
    title: str
    question: str | Unset = ""
    await_id: None | str | Unset = UNSET
    timeout_at: float | None | Unset = UNSET
    waiter_task_ids: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        gate_id = self.gate_id

        gate_type = self.gate_type

        project_id = self.project_id

        title = self.title

        question = self.question

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

        waiter_task_ids: list[str] | Unset = UNSET
        if not isinstance(self.waiter_task_ids, Unset):
            waiter_task_ids = self.waiter_task_ids

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "gate_id": gate_id,
                "gate_type": gate_type,
                "project_id": project_id,
                "title": title,
            }
        )
        if question is not UNSET:
            field_dict["question"] = question
        if await_id is not UNSET:
            field_dict["await_id"] = await_id
        if timeout_at is not UNSET:
            field_dict["timeout_at"] = timeout_at
        if waiter_task_ids is not UNSET:
            field_dict["waiter_task_ids"] = waiter_task_ids

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        gate_id = d.pop("gate_id")

        gate_type = d.pop("gate_type")

        project_id = d.pop("project_id")

        title = d.pop("title")

        question = d.pop("question", UNSET)

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

        waiter_task_ids = cast(list[str], d.pop("waiter_task_ids", UNSET))

        gate_create_payload = cls(
            gate_id=gate_id,
            gate_type=gate_type,
            project_id=project_id,
            title=title,
            question=question,
            await_id=await_id,
            timeout_at=timeout_at,
            waiter_task_ids=waiter_task_ids,
        )

        gate_create_payload.additional_properties = d
        return gate_create_payload

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
