from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitCheckpointArgs")


@_attrs_define
class WaitCheckpointArgs:
    """
    Attributes:
        wait_id (str):
        summary (str):
        project_id (None | str | Unset):
        task_id (None | str | Unset):
        session_id (None | str | Unset):
        claim_epoch (int | None | Unset):
        continuation_id (None | str | Unset):
        generation (int | None | Unset):
    """

    wait_id: str
    summary: str
    project_id: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    session_id: None | str | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET
    continuation_id: None | str | Unset = UNSET
    generation: int | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        wait_id = self.wait_id

        summary = self.summary

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

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        continuation_id: None | str | Unset
        if isinstance(self.continuation_id, Unset):
            continuation_id = UNSET
        else:
            continuation_id = self.continuation_id

        generation: int | None | Unset
        if isinstance(self.generation, Unset):
            generation = UNSET
        else:
            generation = self.generation

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "wait_id": wait_id,
                "summary": summary,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if session_id is not UNSET:
            field_dict["session_id"] = session_id
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch
        if continuation_id is not UNSET:
            field_dict["continuation_id"] = continuation_id
        if generation is not UNSET:
            field_dict["generation"] = generation

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        wait_id = d.pop("wait_id")

        summary = d.pop("summary")

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

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        def _parse_continuation_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        continuation_id = _parse_continuation_id(d.pop("continuation_id", UNSET))

        def _parse_generation(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        generation = _parse_generation(d.pop("generation", UNSET))

        wait_checkpoint_args = cls(
            wait_id=wait_id,
            summary=summary,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            claim_epoch=claim_epoch,
            continuation_id=continuation_id,
            generation=generation,
        )

        return wait_checkpoint_args
